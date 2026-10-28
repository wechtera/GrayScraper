import {
  fetchModels,
  fetchCreators,
  fetchDownloadsStatus,
  fetchDownloadHistory,
  queueDownloads,
  dequeueDownloads,
  controlDownloads,
  runReconcileScan,
  fetchReconcileStatus,
  cancelReconcileScan,
  resetReconcileModels,
  deleteOrphanDirectories,
  ingestUntrackedFiles,
  fetchStagingGarbage,
  purgeStagingGarbage,
  fetchPathDrift,
  realignModelPaths,
  triggerArchivePeekBackfill,
  triggerCatalogPreviewBackfill
} from '../api.js';
import { formatBytes, renderStatusBadge } from '../utils.js';

let currentPendingPage = 1;
const pendingPageSize = 25;
let totalPendingCount = 0;
const selectedPendingIds = new Set();
let pendingCreatorsLoaded = false;

let currentHistoryPage = 1;
const historyPageSize = 25;
let totalHistoryCount = 0;

let activeTransfersTimer = null;
let currentDiscrepancies = [];
let currentOrphanDirectories = [];
let currentUntrackedFiles = [];
let currentStagingGarbage = [];
let currentPathDrifts = [];
let selectedScanMode = 'fast';

function formatDate(val) {
  if (!val) return '-';
  try {
    const s = String(val).replace('T', ' ');
    return s.split(' ')[0] || val;
  } catch (_) {
    return val;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

export function setReconcileMode(mode) {
  selectedScanMode = mode;
  const fastBtn = document.getElementById('btn-mode-fast');
  const deepBtn = document.getElementById('btn-mode-deep');
  if (fastBtn && deepBtn) {
    fastBtn.classList.toggle('active', mode === 'fast');
    deepBtn.classList.toggle('active', mode === 'deep');
  }
  const btnRun = document.getElementById('btn-run-reconcile');
  if (btnRun) {
    btnRun.textContent = `Run ${mode.toUpperCase()} Reconciliation`;
  }
}

export function startActiveTransfersPolling() {
  stopActiveTransfersPolling();

  async function poll() {
    const viewDl = document.getElementById('view-downloads');
    const subActive = document.getElementById('sub-dl-active');

    if (!viewDl || viewDl.style.display === 'none' || !subActive || subActive.style.display === 'none') {
      stopActiveTransfersPolling();
      return;
    }

    const data = await renderActiveTransfers();
    const hasActive = Boolean(
      (data?.active_downloads && data.active_downloads.length > 0) ||
      (data?.active_offloads && data.active_offloads.length > 0) ||
      (data?.counts?.pending > 0) ||
      (data?.counts?.downloading > 0) ||
      (data?.counts?.staging_complete > 0) ||
      (data?.counts?.offloading > 0)
    );

    activeTransfersTimer = setTimeout(poll, hasActive ? 1000 : 2500);
  }

  activeTransfersTimer = setTimeout(poll, 1000);
}

export function stopActiveTransfersPolling() {
  if (activeTransfersTimer) {
    clearTimeout(activeTransfersTimer);
    activeTransfersTimer = null;
  }
}

export function switchDownloadSubView(subview) {
  ['pending', 'active', 'history', 'reconcile', 'sync'].forEach(sv => {
    const el = document.getElementById(`sub-dl-${sv}`);
    if (el) el.style.display = (sv === subview) ? 'block' : 'none';
  });

  document.querySelectorAll('.sub-tabs .sub-btn').forEach(btn => {
    const clickAttr = btn.getAttribute('onclick') || '';
    btn.classList.toggle('active', clickAttr.includes(subview));
  });

  if (subview === 'pending') {
    stopActiveTransfersPolling();
    loadPendingModels();
  } else if (subview === 'active') {
    renderActiveTransfers();
    startActiveTransfersPolling();
  } else if (subview === 'history') {
    stopActiveTransfersPolling();
    loadDownloadHistory();
  } else if (subview === 'reconcile') {
    stopActiveTransfersPolling();
    loadReconcileView();
  } else if (subview === 'sync') {
    stopActiveTransfersPolling();
  }
}

window.switchDownloadSubView = switchDownloadSubView;

export async function populatePendingCreatorsFilter() {
  const select = document.getElementById('pending-creator-filter');
  if (!select) return;

  const currentSelection = select.value;
  try {
    const creators = await fetchCreators();
    let optionsHtml = '<option value="">All Creators</option>';
    optionsHtml += creators.map(c => {
      const name = c.designer_name || 'Unknown';
      const count = c.total_models || 0;
      return `<option value="${escapeHtml(name)}">${escapeHtml(name)} (${count})</option>`;
    }).join('');

    select.innerHTML = optionsHtml;
    if (currentSelection) select.value = currentSelection;
    pendingCreatorsLoaded = true;
  } catch (err) {
    console.error('Failed to populate pending creators:', err);
  }
}

export async function loadPendingModels(page = currentPendingPage) {
  if (!pendingCreatorsLoaded) {
    await populatePendingCreatorsFilter();
  }

  currentPendingPage = Math.max(1, page);

  const searchEl = document.getElementById('pending-search');
  const creatorEl = document.getElementById('pending-creator-filter');
  const sortEl = document.getElementById('pending-sort');
  const hideCompletedEl = document.getElementById('pending-hide-completed');

  const search = searchEl ? searchEl.value.trim() : '';
  const designer = creatorEl ? creatorEl.value : '';
  const sort = sortEl ? sortEl.value : 'date_desc';
  const hideCompleted = hideCompletedEl ? hideCompletedEl.checked : true;

  const offset = (currentPendingPage - 1) * pendingPageSize;
  const tbody = document.getElementById('pending-table-body');
  if (tbody) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-msg">Loading catalog items...</td></tr>';
  }

  try {
    const data = await fetchModels({
      search,
      designer,
      sort,
      hideCompleted,
      limit: pendingPageSize,
      offset
    });

    totalPendingCount = data.total || 0;
    const models = data.models || [];

    if (!tbody) return;

    if (models.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-msg">No matching models found.</td></tr>';
      updatePendingPagination(offset, 0);
      return;
    }

    tbody.innerHTML = models.map(m => {
      const isChecked = selectedPendingIds.has(m.id);
      const isCompleted = m.download_status === 'completed';
      const isQueued = m.download_status === 'queued';

      let actionBtn = '';
      if (isCompleted) {
        actionBtn = '<button type="button" class="btn-pill" disabled>Archived</button>';
      } else if (isQueued) {
        actionBtn = `<button type="button" class="btn-pill btn-action-cancel" onclick="window.dequeueSingleModel(${m.id})">Cancel</button>`;
      } else {
        actionBtn = `<button type="button" class="btn-pill btn-action-queue" onclick="window.queueSingleModel(${m.id})">Queue</button>`;
      }

      const creator = m.designer_name || m.creator_name || m.creator || 'Unknown';
      const fileCount = m.file_count ?? m.total_files ?? m.files ?? 0;
      const dateStr = formatDate(m.published_at || m.date_added);

      return `
        <tr>
          <td class="col-checkbox">
            <input type="checkbox" class="pending-row-chk" value="${m.id}" ${isChecked ? 'checked' : ''} onchange="window.toggleSelectPending(${m.id}, this.checked)">
          </td>
          <td>
            <div class="cell-clickable-title" onclick="window.openModelDetailModal(${m.id})" title="View Metadata">
              ${escapeHtml(m.name)}
            </div>
            <span class="font-mono text-dim font-xs">#${m.id}</span>
          </td>
          <td>
            <span class="cell-creator-accent">${escapeHtml(creator)}</span>
          </td>
          <td>
            <span class="font-semibold font-mono">${fileCount}</span>
          </td>
          <td>
            <span class="font-mono text-dim font-sm">${dateStr}</span>
          </td>
          <td>${renderStatusBadge(m.download_status || 'not_downloaded')}</td>
          <td>${actionBtn}</td>
        </tr>
      `;
    }).join('');

    updatePendingPagination(offset, models.length);
    updateSelectAllCheckbox(models);
  } catch (err) {
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="7" class="table-msg text-alert">Failed loading models: ${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

export function resetPendingPageAndLoad() {
  loadPendingModels(1);
}

export function changePendingPage(delta) {
  const totalPages = Math.ceil(totalPendingCount / pendingPageSize) || 1;
  const target = currentPendingPage + delta;
  if (target >= 1 && target <= totalPages) {
    loadPendingModels(target);
  }
}

function updatePendingPagination(offset, count) {
  const pageInfo = document.getElementById('pending-page-info');
  const pageNum = document.getElementById('pending-page-num');
  const prevBtn = document.getElementById('pending-prev-btn');
  const nextBtn = document.getElementById('pending-next-btn');

  const totalPages = Math.ceil(totalPendingCount / pendingPageSize) || 1;
  const start = totalPendingCount === 0 ? 0 : offset + 1;
  const end = offset + count;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalPendingCount.toLocaleString()} models`;
  if (pageNum) pageNum.textContent = `Page ${currentPendingPage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentPendingPage <= 1;
  if (nextBtn) nextBtn.disabled = currentPendingPage >= totalPages;
}

export function toggleSelectPending(modelId, isChecked) {
  if (isChecked) {
    selectedPendingIds.add(modelId);
  } else {
    selectedPendingIds.delete(modelId);
  }
  updateSelectedCount();
}

export function toggleSelectAllPending(isChecked) {
  const checkboxes = document.querySelectorAll('.pending-row-chk');
  checkboxes.forEach(chk => {
    const id = parseInt(chk.value, 10);
    chk.checked = isChecked;
    if (isChecked) {
      selectedPendingIds.add(id);
    } else {
      selectedPendingIds.delete(id);
    }
  });
  updateSelectedCount();
}

function updateSelectAllCheckbox(currentModels) {
  const selectAll = document.getElementById('select-all-pending');
  if (!selectAll || currentModels.length === 0) return;
  const allChecked = currentModels.every(m => selectedPendingIds.has(m.id));
  selectAll.checked = allChecked;
}

function updateSelectedCount() {
  const counter = document.getElementById('selected-count');
  if (counter) counter.textContent = selectedPendingIds.size;
}

export async function queueSelectedModels() {
  if (selectedPendingIds.size === 0) {
    alert('Please select at least one model to queue.');
    return;
  }
  const ids = Array.from(selectedPendingIds);
  try {
    await queueDownloads({ model_ids: ids });
    selectedPendingIds.clear();
    updateSelectedCount();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Failed to queue models: ${err.message}`);
  }
}

export async function dequeueSelectedModels() {
  if (selectedPendingIds.size === 0) {
    alert('Please select at least one model to remove.');
    return;
  }
  const ids = Array.from(selectedPendingIds);
  try {
    await dequeueDownloads({ model_ids: ids });
    selectedPendingIds.clear();
    updateSelectedCount();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Failed to remove models: ${err.message}`);
  }
}

export async function queueSingleModel(modelId) {
  try {
    await queueDownloads({ model_ids: [modelId] });
    window.refreshCurrentView();
  } catch (err) {
    alert(`Queue failed: ${err.message}`);
  }
}

export async function dequeueSingleModel(modelId) {
  try {
    await dequeueDownloads({ model_ids: [modelId] });
    window.refreshCurrentView();
  } catch (err) {
    alert(`Cancel failed: ${err.message}`);
  }
}

export async function clearQueueStatus(status = 'pending') {
  if (!confirm(`Are you sure you want to clear all ${status} jobs from the queue?`)) return;
  try {
    await dequeueDownloads({ clear_status: status });
    selectedPendingIds.clear();
    updateSelectedCount();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Clear queue failed: ${err.message}`);
  }
}

export async function confirmPurgeIncomplete() {
  if (!confirm('Purge all pending and downloading jobs? Completed files will remain intact.')) return;
  try {
    await dequeueDownloads({ purge_incomplete: true });
    renderActiveTransfers();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Purge failed: ${err.message}`);
  }
}

export async function renderActiveTransfers() {
  try {
    const data = await fetchDownloadsStatus();

    const stateEl = document.getElementById('active-queue-state');
    const pendEl = document.getElementById('active-queue-pending');
    const stageEl = document.getElementById('active-queue-staged');

    if (stateEl) {
      stateEl.textContent = data.is_paused ? 'PAUSED' : (data.is_running ? 'RUNNING' : 'STOPPED');
      stateEl.className = data.is_paused ? 'text-warn font-bold' : (data.is_running ? 'text-accent font-bold' : 'text-dim font-bold');
    }
    if (pendEl) pendEl.textContent = data.counts?.pending || 0;
    if (stageEl) stageEl.textContent = data.counts?.staging_complete || 0;

    const dlBox = document.getElementById('active-download-workers-container');
    if (dlBox) {
      const activeDownloads = data.active_downloads || [];
      if (activeDownloads.length === 0) {
        dlBox.innerHTML = '<div class="empty-state-notice">No active downloads in flight.</div>';
      } else {
        dlBox.innerHTML = activeDownloads.map(w => {
          const pct = w.total_bytes > 0 ? Math.min(100, Math.round((w.downloaded_bytes / w.total_bytes) * 100)) : 0;
          return `
            <div class="progress-box">
              <div class="progress-box-top">
                <span><strong>${escapeHtml(w.model_name)}</strong> &bull; ${escapeHtml(w.current_file || '')}</span>
                <span class="font-mono">${formatBytes(w.downloaded_bytes)} / ${formatBytes(w.total_bytes)} (${pct}%)</span>
              </div>
              <div class="progress-bar-bg progress-bar-flush">
                <div class="progress-bar-fill" style="width:${pct}%"></div>
              </div>
              <div class="progress-box-meta font-mono">
                Speed: ${formatBytes(w.speed_bps || 0)}/s &bull; Creator: ${escapeHtml(w.creator || 'Unknown')}
              </div>
            </div>
          `;
        }).join('');
      }
    }

    const offBox = document.getElementById('active-offload-workers-container');
    if (offBox) {
      const activeOffloads = data.active_offloads || [];
      if (activeOffloads.length === 0) {
        offBox.innerHTML = '<div class="empty-state-notice">No offload migrations in progress.</div>';
      } else {
        offBox.innerHTML = activeOffloads.map(w => {
          const pct = w.total_bytes > 0 ? Math.min(100, Math.round((w.transferred_bytes / w.total_bytes) * 100)) : 0;
          return `
            <div class="progress-box">
              <div class="progress-box-top">
                <span><strong>${escapeHtml(w.model_name)}</strong> &bull; ${escapeHtml(w.current_file || '')}</span>
                <span class="font-mono">${formatBytes(w.transferred_bytes)} / ${formatBytes(w.total_bytes)} (${pct}%)</span>
              </div>
              <div class="progress-bar-bg progress-bar-flush">
                <div class="progress-bar-fill" style="width:${pct}%"></div>
              </div>
              <div class="progress-box-meta font-mono">
                LAN Transfer Speed: ${formatBytes(w.speed_bps || 0)}/s
              </div>
            </div>
          `;
        }).join('');
      }
    }

    const activeTbody = document.getElementById('active-pending-table-body');
    if (activeTbody) {
      const jobs = data.queued_jobs || [];
      if (jobs.length === 0) {
        activeTbody.innerHTML = '<tr><td colspan="8" class="td-empty">No jobs currently queued.</td></tr>';
      } else {
        activeTbody.innerHTML = jobs.map(j => `
          <tr>
            <td class="font-mono">${j.model_id}</td>
            <td><strong>${escapeHtml(j.model_name || `Model #${j.model_id}`)}</strong></td>
            <td>${escapeHtml(j.creator_name || 'Unknown')}</td>
            <td class="font-mono">${j.total_files || 0}</td>
            <td class="font-mono">${formatBytes(j.total_bytes || 0)}</td>
            <td class="font-mono text-dim font-sm">${formatDate(j.enqueued_at)}</td>
            <td>${renderStatusBadge(j.status)}</td>
            <td>
              <button type="button" class="btn-pill btn-action-cancel" onclick="window.dequeueSingleModel(${j.model_id})">Cancel</button>
            </td>
          </tr>
        `).join('');
      }
    }

    return data;
  } catch (err) {
    console.error('Failed active transfers poll:', err);
    return null;
  }
}

export async function controlDownload(action) {
  try {
    await controlDownloads(action);
    renderActiveTransfers();
  } catch (err) {
    alert(`Control action failed: ${err.message}`);
  }
}

export async function loadDownloadHistory(page = currentHistoryPage) {
  currentHistoryPage = Math.max(1, page);

  const filterEl = document.getElementById('history-filter');
  const sortEl = document.getElementById('history-sort');

  const status = filterEl ? filterEl.value : '';
  const sort = sortEl ? sortEl.value : 'enqueued_desc';

  const offset = (currentHistoryPage - 1) * historyPageSize;
  const tbody = document.getElementById('history-table-body');

  if (tbody) {
    tbody.innerHTML = '<tr><td colspan="8" class="table-msg">Loading audit logs...</td></tr>';
  }

  try {
    const data = await fetchDownloadHistory({
      status,
      sort,
      limit: historyPageSize,
      offset
    });

    totalHistoryCount = data.total || 0;
    const jobs = data.history || [];

    if (!tbody) return;

    if (jobs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="table-msg">No historical jobs recorded.</td></tr>';
      updateHistoryPagination(offset, 0);
      return;
    }

    tbody.innerHTML = jobs.map(j => {
      const creator = j.creator_name || j.designer_name || 'Unknown';
      return `
        <tr>
          <td class="font-mono text-dim font-sm">${formatDate(j.enqueued_at)}</td>
          <td class="font-mono text-dim font-sm">${formatDate(j.completed_at)}</td>
          <td>
            <div class="cell-clickable-title" onclick="window.openModelDetailModal(${j.model_id})">
              ${escapeHtml(j.model_name || `Model #${j.model_id}`)}
            </div>
          </td>
          <td>${escapeHtml(creator)}</td>
          <td>${renderStatusBadge(j.status)}</td>
          <td class="font-mono">${j.retry_count || 0}</td>
          <td class="cell-label-truncate font-mono font-xs" title="${escapeHtml(j.error_message || '')}">
            ${escapeHtml(j.error_message || '-')}
          </td>
          <td>
            ${j.status === 'failed'
              ? `<button type="button" class="btn-pill btn-action-queue" onclick="window.queueSingleModel(${j.model_id})">Retry</button>`
              : '<span class="text-dim font-sm">None</span>'
            }
          </td>
        </tr>
      `;
    }).join('');

    updateHistoryPagination(offset, jobs.length);
  } catch (err) {
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="8" class="table-msg text-alert">Failed to load history: ${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

export function resetHistoryPageAndLoad() {
  loadDownloadHistory(1);
}

export function changeHistoryPage(delta) {
  const totalPages = Math.ceil(totalHistoryCount / historyPageSize) || 1;
  const target = currentHistoryPage + delta;
  if (target >= 1 && target <= totalPages) {
    loadDownloadHistory(target);
  }
}

function updateHistoryPagination(offset, count) {
  const pageInfo = document.getElementById('history-page-info');
  const pageNum = document.getElementById('history-page-num');
  const prevBtn = document.getElementById('history-prev-btn');
  const nextBtn = document.getElementById('history-next-btn');

  const totalPages = Math.ceil(totalHistoryCount / historyPageSize) || 1;
  const start = totalHistoryCount === 0 ? 0 : offset + 1;
  const end = offset + count;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalHistoryCount.toLocaleString()} jobs`;
  if (pageNum) pageNum.textContent = `Page ${currentHistoryPage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentHistoryPage <= 1;
  if (nextBtn) nextBtn.disabled = currentHistoryPage >= totalPages;
}

// -----------------------------------------------------------------------------
// Reconciliation Sub-View & Telemetry Consumer
// -----------------------------------------------------------------------------

export function updateReconcileFromHealth(task) {
  if (!task || task.task_type !== 'vault_reconcile') return;

  const progressBox = document.getElementById('reconcile-progress-container');
  const progressBar = document.getElementById('reconcile-progress-bar');
  const progressPct = document.getElementById('reconcile-progress-pct');
  const currentItemEl = document.getElementById('reconcile-current-item');
  const statusBadge = document.getElementById('reconcile-status-badge');
  const logsEl = document.getElementById('reconcile-task-logs');
  const btnRun = document.getElementById('btn-run-reconcile');

  const scanMode = (task.params?.scan_mode || selectedScanMode).toUpperCase();

  if (task.status === 'running') {
    if (progressBox) progressBox.style.display = 'block';
    if (statusBadge) {
      statusBadge.textContent = `${scanMode} SCAN`;
      statusBadge.className = 'status-badge status-queued';
    }
    if (btnRun) {
      btnRun.disabled = true;
      btnRun.textContent = `${scanMode} Audit Running...`;
    }

    const pct = Math.min(100, Math.max(0, task.progress_pct || 0));
    if (progressBar) progressBar.style.width = `${pct}%`;
    if (progressPct) progressPct.textContent = `${pct}%`;
    if (currentItemEl) currentItemEl.textContent = task.progress_label || 'Auditing storage...';

    if (logsEl && task.log_output) {
      logsEl.textContent = task.log_output;
      logsEl.scrollTop = logsEl.scrollHeight;
    }
  } else if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
    if (btnRun) {
      btnRun.disabled = false;
      btnRun.textContent = `Run ${selectedScanMode.toUpperCase()} Reconciliation`;
    }
    loadReconcileView();
  }
}

export async function loadReconcileView() {
  const progressBox = document.getElementById('reconcile-progress-container');
  const btnRun = document.getElementById('btn-run-reconcile');
  const lastScanEl = document.getElementById('reconcile-last-scan');
  const fixAllBtn = document.getElementById('btn-fix-all-reconcile');
  const orphanSection = document.getElementById('reconcile-orphan-section');

  try {
    const data = await fetchReconcileStatus();

    if (data.status === 'running') {
      updateReconcileFromHealth({
        task_type: 'vault_reconcile',
        status: 'running',
        progress_pct: data.progress_pct,
        progress_label: data.current_item,
        log_output: data.log_output,
        params: { scan_mode: data.scan_mode }
      });
      return data;
    }

    if (progressBox) progressBox.style.display = 'none';
    if (btnRun) {
      btnRun.disabled = false;
      btnRun.textContent = `Run ${selectedScanMode.toUpperCase()} Reconciliation`;
    }

    const summary = data.summary || {};
    currentDiscrepancies = data.discrepancies || [];
    currentOrphanDirectories = data.orphan_directories || [];
    currentUntrackedFiles = data.untracked_files || [];

    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setVal('reconcile-stat-audited', (summary.total_audited || 0).toLocaleString());
    setVal('reconcile-stat-healthy', (summary.healthy || 0).toLocaleString());
    setVal('reconcile-stat-discrepancies', (summary.discrepancies || 0).toLocaleString());

    const countSpan = document.getElementById('reconcile-defect-count');
    if (countSpan) countSpan.textContent = currentDiscrepancies.length;

    if (fixAllBtn) {
      fixAllBtn.style.display = currentDiscrepancies.length > 0 ? 'inline-flex' : 'none';
    }

    if (lastScanEl) {
      if (data.timestamp) {
        const timeStr = data.timestamp.replace('T', ' ').split('.')[0];
        let msg = `Last ${(data.scan_mode || 'fast').toUpperCase()} scan finished at ${timeStr}.`;
        if (summary.hashes_baselined > 0) {
          msg += ` Established & stored ${summary.hashes_baselined} new SHA-256 checksums.`;
        }
        lastScanEl.textContent = msg;
      } else {
        lastScanEl.textContent = 'Audit not run yet. Select mode and click "Run Forward Reconciliation".';
      }
    }

    if (data.scan_mode === 'reverse' || currentOrphanDirectories.length > 0 || currentUntrackedFiles.length > 0) {
      if (orphanSection) {
        orphanSection.style.display = 'block';
        renderReverseOrphanSection(orphanSection, currentOrphanDirectories, currentUntrackedFiles);
      }
    } else if (orphanSection) {
      orphanSection.style.display = 'none';
    }

    renderReconcileDiscrepancies(currentDiscrepancies);
    return data;
  } catch (err) {
    console.error('Failed to load reconcile view:', err);
    return null;
  }
}

export async function triggerReconcileScan() {
  const btn = document.getElementById('btn-run-reconcile');
  const progressBox = document.getElementById('reconcile-progress-container');
  const logsEl = document.getElementById('reconcile-task-logs');

  if (btn) {
    btn.disabled = true;
    btn.textContent = `Dispatching ${selectedScanMode.toUpperCase()} Task...`;
  }
  if (progressBox) progressBox.style.display = 'block';
  if (logsEl) logsEl.textContent = `Dispatching ${selectedScanMode.toUpperCase()} task to background worker...`;

  try {
    const res = await runReconcileScan(selectedScanMode);
    if (!res.success) {
      throw new Error(res.error || 'Failed to dispatch task');
    }
  } catch (err) {
    alert(`Failed launching reconciliation: ${err.message}`);
    if (progressBox) progressBox.style.display = 'none';
    if (btn) {
      btn.disabled = false;
      btn.textContent = `Run ${selectedScanMode.toUpperCase()} Reconciliation`;
    }
  }
}

export async function triggerOrphanAudit() {
  const btn = document.getElementById('btn-run-orphan-audit');
  const progressBox = document.getElementById('reconcile-progress-container');
  const logsEl = document.getElementById('reconcile-task-logs');

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Dispatching Reverse Audit...';
  }
  if (progressBox) progressBox.style.display = 'block';
  if (logsEl) logsEl.textContent = 'Dispatching reverse orphan audit to background pipeline worker...';

  try {
    const res = await runReconcileScan('reverse');
    if (!res.success) throw new Error(res.error || 'Failed to dispatch orphan audit');
  } catch (err) {
    alert(`Failed launching orphan audit: ${err.message}`);
    if (progressBox) progressBox.style.display = 'none';
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Run Reverse Orphan Audit';
    }
  }
}

export async function cancelReconcile() {
  try {
    await cancelReconcileScan();
  } catch (err) {
    alert(`Cancel request failed: ${err.message}`);
  }
}

function renderReverseOrphanSection(container, orphans, untracked) {
  if (orphans.length === 0 && untracked.length === 0) {
    container.innerHTML = '';
    return;
  }

  let html = '';
  if (orphans.length > 0) {
    html += `
      <div class="orphan-header-row">
        <div class="orphan-header-title">
          Reverse Audit: Orphan Directories on Disk (${orphans.length})
        </div>
        <button type="button" class="btn-pill btn-action-cancel" onclick="window.deleteAllOrphans()">
          Delete All Orphan Folders (${orphans.length})
        </button>
      </div>
    `;
    html += orphans.map(o => `
      <div class="card reconcile-card card-orphan">
        <div class="reconcile-card-header">
          <div>
            <div class="model-title">${escapeHtml(o.folder_name)}</div>
            <div class="font-mono text-dim path-text">${escapeHtml(o.directory_path)}</div>
          </div>
          <div class="footer-actions">
            <span class="font-mono text-dim font-sm">${formatBytes(o.size_bytes)}</span>
            <button type="button" class="btn-pill btn-action-cancel" onclick="window.deleteSingleOrphan('${escapeHtml(o.directory_path).replace(/'/g, "\\'")}')">
              Delete Directory
            </button>
          </div>
        </div>
        <div class="defect-detail text-alert">${escapeHtml(o.detail)}</div>
      </div>
    `).join('');
  }

  if (untracked.length > 0) {
    html += `
      <div class="orphan-header-row">
        <div class="untracked-header-title">
          Reverse Audit: Untracked Files Inside Valid Models (${untracked.length})
        </div>
        <button type="button" class="btn-pill btn-action-primary" onclick="window.ingestAllUntracked()">
          Auto-Ingest All Untracked Files (${untracked.length})
        </button>
      </div>
    `;
    html += untracked.map(u => `
      <div class="card reconcile-card card-untracked">
        <div class="reconcile-card-header">
          <div>
            <div class="cell-clickable-title" onclick="window.openModelDetailModal(${u.model_id})">
              ${escapeHtml(u.model_name)} <span class="font-mono text-dim font-xs">(#${u.model_id})</span>
            </div>
            <div class="model-subtitle">
              File: <strong class="text-main">${escapeHtml(u.filename)}</strong> (${formatBytes(u.size_bytes)})
            </div>
          </div>
          <button type="button" class="btn-pill btn-action-primary" onclick="window.ingestSingleUntracked(${u.model_id}, '${escapeHtml(u.file_path).replace(/'/g, "\\'")}')">
            Auto-Ingest to DB
          </button>
        </div>
        <div class="defect-detail text-accent">${escapeHtml(u.detail)}</div>
      </div>
    `).join('');
  }

  container.innerHTML = html;
}

function renderReconcileDiscrepancies(items) {
  const container = document.getElementById('reconcile-results-container');
  if (!container) return;

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div class="results-placeholder text-accent">
        100% Integrity Confirmed: All audited models have valid directories, expected files, matching sizes, and uncorrupted archive checksums.
      </div>
    `;
    return;
  }

  container.innerHTML = items.map(m => {
    const issuesHtml = m.issues.map(iss => `
      <div class="reconcile-issue-row">
        <div class="issue-top">
          <span class="reconcile-defect-pill">${escapeHtml(iss.defect)}</span>
          <span class="font-mono text-dim font-xs">${escapeHtml(iss.filename || 'Directory Defect')}</span>
        </div>
        <div class="defect-detail text-alert font-sm">
          ${escapeHtml(iss.detail)}
        </div>
        <div class="font-mono text-dim font-xs">
          Expected: ${escapeHtml(iss.expected_path)}
        </div>
      </div>
    `).join('');

    return `
      <div class="card reconcile-card">
        <div class="reconcile-card-header">
          <div>
            <div class="cell-clickable-title" onclick="window.openModelDetailModal(${m.model_id})">
              ${escapeHtml(m.model_name)} <span class="font-mono text-dim font-xs">(#${m.model_id})</span>
            </div>
            <div class="model-subtitle">
              Creator: ${escapeHtml(m.creator_name)}
            </div>
          </div>
          <button type="button" class="btn-pill btn-action-cancel" onclick="window.resetSingleReconcileModel(${m.model_id})">
            Reset for Retry
          </button>
        </div>
        <div class="reconcile-card-body">
          ${issuesHtml}
        </div>
      </div>
    `;
  }).join('');
}

export async function resetSingleReconcileModel(modelId) {
  if (!confirm(`Reset Model #${modelId} in database? Its vault record will be cleared and the job set to 'failed' so it can be re-downloaded.`)) return;
  try {
    const res = await resetReconcileModels([modelId]);
    if (res.success) {
      currentDiscrepancies = currentDiscrepancies.filter(d => d.model_id !== modelId);
      renderReconcileDiscrepancies(currentDiscrepancies);
      const countSpan = document.getElementById('reconcile-defect-count');
      if (countSpan) countSpan.textContent = currentDiscrepancies.length;
      const fixAllBtn = document.getElementById('btn-fix-all-reconcile');
      if (fixAllBtn) fixAllBtn.style.display = currentDiscrepancies.length > 0 ? 'inline-flex' : 'none';
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Reset failed: ${err.message}`);
  }
}

export async function resetAllReconcileDiscrepancies() {
  if (!currentDiscrepancies || currentDiscrepancies.length === 0) return;
  const count = currentDiscrepancies.length;
  if (!confirm(`Reset all ${count} discrepant models in the database? Their vault records will be cleared and jobs placed in 'failed' status for batch retry.`)) return;

  const ids = currentDiscrepancies.map(d => d.model_id);
  try {
    const res = await resetReconcileModels(ids);
    if (res.success) {
      currentDiscrepancies = [];
      renderReconcileDiscrepancies([]);
      const countSpan = document.getElementById('reconcile-defect-count');
      if (countSpan) countSpan.textContent = '0';
      const fixAllBtn = document.getElementById('btn-fix-all-reconcile');
      if (fixAllBtn) fixAllBtn.style.display = 'none';
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Reset all failed: ${err.message}`);
  }
}

export async function deleteSingleOrphan(path) {
  if (!confirm(`Permanently delete orphan directory from disk?\n\n${path}`)) return;
  try {
    const res = await deleteOrphanDirectories([path]);
    if (res.success) {
      currentOrphanDirectories = currentOrphanDirectories.filter(o => o.directory_path !== path);
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), currentOrphanDirectories, currentUntrackedFiles);
    }
  } catch (err) {
    alert(`Failed deleting directory: ${err.message}`);
  }
}

export async function deleteAllOrphans() {
  if (currentOrphanDirectories.length === 0) return;
  const count = currentOrphanDirectories.length;
  if (!confirm(`Permanently delete all ${count} orphan directories from disk? This cannot be undone.`)) return;

  const paths = currentOrphanDirectories.map(o => o.directory_path);
  try {
    const res = await deleteOrphanDirectories(paths);
    if (res.success) {
      currentOrphanDirectories = [];
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), [], currentUntrackedFiles);
    }
  } catch (err) {
    alert(`Failed deleting orphan directories: ${err.message}`);
  }
}

export async function ingestSingleUntracked(modelId, filePath) {
  try {
    const res = await ingestUntrackedFiles([{ model_id: modelId, file_path: filePath }]);
    if (res.success) {
      currentUntrackedFiles = currentUntrackedFiles.filter(u => u.file_path !== filePath);
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), currentOrphanDirectories, currentUntrackedFiles);
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Failed ingesting file: ${err.message}`);
  }
}

export async function ingestAllUntracked() {
  if (currentUntrackedFiles.length === 0) return;
  const count = currentUntrackedFiles.length;
  if (!confirm(`Auto-ingest all ${count} untracked files into the database?`)) return;

  const items = currentUntrackedFiles.map(u => ({ model_id: u.model_id, file_path: u.file_path }));
  try {
    const res = await ingestUntrackedFiles(items);
    if (res.success) {
      currentUntrackedFiles = [];
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), currentOrphanDirectories, []);
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Failed ingesting untracked files: ${err.message}`);
  }
}

export async function triggerStagingSweep() {
  const container = document.getElementById('reconcile-results-container');
  if (container) {
    container.innerHTML = '<div class="table-msg">Scanning local scratch staging directory (.work/in_flight & .work/staged)...</div>';
  }
  try {
    const res = await fetchStagingGarbage();
    if (!res.success) throw new Error(res.error || 'Failed scanning staging.');
    currentStagingGarbage = res.items || [];
    renderStagingGarbageResults(res.total_bytes, currentStagingGarbage);
  } catch (err) {
    alert(`Staging scan failed: ${err.message}`);
  }
}

function renderStagingGarbageResults(totalBytes, items) {
  const container = document.getElementById('reconcile-results-container');
  if (!container) return;

  if (items.length === 0) {
    container.innerHTML = `
      <div class="results-placeholder text-accent">
        Staging Clean: Zero abandoned .part files or orphaned staged model folders found.
      </div>
    `;
    return;
  }

  let html = `
    <div class="orphan-header-row">
      <div>
        <span class="findings-title-alert">Staging Garbage Discovered (${items.length} items)</span>
        <span class="font-sm text-dim ml-2">Total Reclaimable: <strong>${formatBytes(totalBytes)}</strong></span>
      </div>
      <button type="button" class="btn-pill btn-action-cancel" onclick="window.purgeAllStagingGarbage()">
        Purge All Staging Garbage (${formatBytes(totalBytes)})
      </button>
    </div>
  `;

  html += items.map(it => `
    <div class="card reconcile-card card-orphan">
      <div class="reconcile-card-header">
        <div>
          <div class="model-title">
            ${it.type === 'in_flight_part' ? 'In-Flight .part File' : 'Staged Model Folder'}: ${escapeHtml(it.name)}
          </div>
          <div class="font-mono text-dim path-text">${escapeHtml(it.path)}</div>
        </div>
        <div class="footer-actions">
          <span class="font-mono text-dim font-sm">${formatBytes(it.size_bytes)}</span>
          <button type="button" class="btn-pill btn-action-cancel" onclick="window.purgeSingleStagingGarbage('${escapeHtml(it.path).replace(/'/g, "\\'")}')">Purge</button>
        </div>
      </div>
      <div class="defect-detail text-alert">${escapeHtml(it.reason)}</div>
    </div>
  `).join('');

  container.innerHTML = html;
}

export async function purgeSingleStagingGarbage(path) {
  if (!confirm(`Purge this staging file/directory?\n\n${path}`)) return;
  try {
    const res = await purgeStagingGarbage([path]);
    if (res.success) {
      currentStagingGarbage = currentStagingGarbage.filter(it => it.path !== path);
      const remainingBytes = currentStagingGarbage.reduce((acc, it) => acc + (it.size_bytes || 0), 0);
      renderStagingGarbageResults(remainingBytes, currentStagingGarbage);
    }
  } catch (err) {
    alert(`Failed purging: ${err.message}`);
  }
}

export async function purgeAllStagingGarbage() {
  if (currentStagingGarbage.length === 0) return;
  if (!confirm(`Permanently purge all ${currentStagingGarbage.length} stale staging files/folders?`)) return;
  try {
    const res = await purgeStagingGarbage();
    if (res.success) {
      currentStagingGarbage = [];
      renderStagingGarbageResults(0, []);
    }
  } catch (err) {
    alert(`Failed purging staging: ${err.message}`);
  }
}

export async function triggerPathDriftScan() {
  const container = document.getElementById('reconcile-results-container');
  if (container) {
    container.innerHTML = '<div class="table-msg">Scanning for legacy naming schemes and path drift across vault...</div>';
  }
  try {
    const res = await fetchPathDrift();
    if (!res.success) throw new Error(res.error || 'Failed scanning path drift.');
    currentPathDrifts = res.items || [];
    renderPathDriftResults(currentPathDrifts);
  } catch (err) {
    alert(`Path drift scan failed: ${err.message}`);
  }
}

function renderPathDriftResults(items) {
  const container = document.getElementById('reconcile-results-container');
  if (!container) return;

  if (items.length === 0) {
    container.innerHTML = `
      <div class="results-placeholder text-accent">
        Zero Path Drift: All archived models conform to canonical naming standards.
      </div>
    `;
    return;
  }

  let html = `
    <div class="orphan-header-row">
      <div>
        <span class="findings-title-drift">Legacy Path Drift Detected (${items.length} models)</span>
        <div class="font-sm text-dim">Folders with brackets, unescaped whitespace, or obsolete creator paths.</div>
      </div>
      <button type="button" class="btn-pill btn-action-primary" onclick="window.realignAllPaths()">
        Realign All Models (${items.length})
      </button>
    </div>
  `;

  html += items.map(it => `
    <div class="card reconcile-card card-drift">
      <div class="reconcile-card-header">
        <div>
          <div class="cell-clickable-title" onclick="window.openModelDetailModal(${it.model_id})">
            ${escapeHtml(it.model_name)} <span class="font-mono text-dim font-xs">(#${it.model_id})</span>
          </div>
          <div class="model-subtitle font-mono">${it.reasons.join(' &bull; ')}</div>
        </div>
        <button type="button" class="btn-pill btn-action-primary" onclick="window.realignSinglePath(${it.model_id})">Realign Path</button>
      </div>
      <div>
        <div class="path-text-current font-mono"><strong>Current:</strong> ${escapeHtml(it.current_path)}</div>
        <div class="path-text-canonical font-mono"><strong>Canonical:</strong> ${escapeHtml(it.canonical_path)}</div>
      </div>
    </div>
  `).join('');

  container.innerHTML = html;
}

export async function realignSinglePath(modelId) {
  try {
    const res = await realignModelPaths([modelId]);
    if (res.success) {
      currentPathDrifts = currentPathDrifts.filter(it => it.model_id !== modelId);
      renderPathDriftResults(currentPathDrifts);
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Realign failed: ${err.message}`);
  }
}

export async function realignAllPaths() {
  if (currentPathDrifts.length === 0) return;
  const count = currentPathDrifts.length;
  if (!confirm(`Realign all ${count} legacy model directories to canonical sanitized paths and update the database?`)) return;

  const ids = currentPathDrifts.map(it => it.model_id);
  try {
    const res = await realignModelPaths(ids);
    if (res.success) {
      currentPathDrifts = [];
      renderPathDriftResults([]);
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Realign all failed: ${err.message}`);
  }
}

// Add the backfill controller function:
export async function triggerArchiveInspectionSweep() {
  const btn = document.getElementById('btn-backfill-archives');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Launching Archive Peek...';
  }

  try {
    const res = await triggerArchivePeekBackfill();
    if (!res.success) throw new Error(res.error || 'Failed to dispatch archive peek backfill');
    alert('Archive peek backfill dispatched to background pipeline worker. You can monitor progress in Pipeline Tasks.');
    if (typeof window.checkHealth === 'function') {
      window.checkHealth();
    }
  } catch (err) {
    alert(`Archive peek backfill failed: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Backfill Archive Inspection';
    }
  }
}

export { resetAllReconcileDiscrepancies as resetAllReconcileDiscrepencies };


export async function addSelectedToStash() {
  if (selectedPendingIds.size === 0) {
    alert('Please select at least one model to add to stash.');
    return;
  }
  const ids = Array.from(selectedPendingIds);

  if (typeof window.openAddToStashModal === 'function') {
    window.openAddToStashModal({
      item_type: 'bulk_models',
      model_ids: ids,
      title: `${ids.length} Selected Model${ids.length === 1 ? '' : 's'}`
    }, () => {
      // Clear selection upon successful stash addition
      selectedPendingIds.clear();
      updateSelectedCount();
      const selectAll = document.getElementById('select-all-pending');
      if (selectAll) selectAll.checked = false;
      document.querySelectorAll('.pending-row-chk').forEach(chk => { chk.checked = false; });
    });
  }
}

// Bind to window for HTML onclick handler
window.addSelectedToStash = addSelectedToStash;


export async function triggerCatalogPreviewSweep() {
  const btn = document.getElementById('btn-backfill-previews');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Caching Previews...';
  }

  try {
    const res = await triggerCatalogPreviewBackfill();
    if (!res.success) throw new Error(res.error || 'Failed to dispatch catalog preview backfill');
    alert('Catalog preview cache sweep dispatched to background pipeline worker. Monitor progress in Pipeline Tasks.');
    if (typeof window.checkHealth === 'function') {
      window.checkHealth();
    }
  } catch (err) {
    alert(`Preview cache failed: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Backfill Model Previews';
    }
  }
}

window.triggerCatalogPreviewSweep = triggerCatalogPreviewSweep;
