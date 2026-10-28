import {
  fetchTasksStatus,
  cancelTask,
  killTask,
  clearTasksHistory,
  startScraper,
  fetchScrapesList,
  startIngest
} from '../api.js';
import { escapeHtml } from '../utils.js';

export async function renderTasksView() {
  try {
    const data = await fetchTasksStatus();
    const active = data.active_task;
    const recents = data.recent_tasks || [];

    const statusBadge = document.getElementById('task-status-badge');
    const typeBadge = document.getElementById('task-type-badge');
    const cancelBtn = document.getElementById('btn-cancel-active-task');
    const killBtn = document.getElementById('btn-kill-active-task');
    const progressBar = document.getElementById('task-progress-bar');
    const progressLabel = document.getElementById('task-progress-label');
    const progressPct = document.getElementById('task-progress-pct');
    const logViewer = document.getElementById('task-live-logs');
    const autoscrollChk = document.getElementById('chk-log-autoscroll');

    if (active) {
      const statusUpper = (active.status || 'RUNNING').toUpperCase();
      if (statusBadge) {
        statusBadge.textContent = statusUpper;
        statusBadge.className = `status-badge ${getStatusBadgeClass(active.status)}`;
      }

      const typeLabel = active.task_type === 'library_scrape'
        ? 'Library Scraper'
        : (active.task_type === 'metadata_ingest' ? 'Metadata Ingestion' : active.task_type);

      if (typeBadge) typeBadge.textContent = `Task #${active.id} - ${typeLabel}`;
      if (cancelBtn) cancelBtn.style.display = 'inline-flex';
      if (killBtn) killBtn.style.display = 'inline-flex';

      const pct = Math.min(100, Math.max(0, active.progress_pct || 0));
      if (progressBar) progressBar.style.width = `${pct}%`;
      if (progressPct) progressPct.textContent = `${pct}%`;
      if (progressLabel) progressLabel.textContent = active.progress_label || 'Processing...';

      if (logViewer && active.log_output) {
        logViewer.textContent = active.log_output;
        if (autoscrollChk && autoscrollChk.checked) {
          logViewer.scrollTop = logViewer.scrollHeight;
        }
      }
    } else {
      if (statusBadge) {
        statusBadge.textContent = 'IDLE';
        statusBadge.className = 'status-badge status-not_downloaded';
      }
      if (typeBadge) typeBadge.textContent = 'No background tasks running';
      if (cancelBtn) cancelBtn.style.display = 'none';
      if (killBtn) killBtn.style.display = 'none';
      if (progressBar) progressBar.style.width = '0%';
      if (progressPct) progressPct.textContent = '0%';
      if (progressLabel) progressLabel.textContent = 'Worker idle and waiting for jobs';
    }

    const tbody = document.getElementById('task-history-tbody');
    if (tbody) {
      if (recents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="td-empty">No recent tasks recorded.</td></tr>';
      } else {
        tbody.innerHTML = recents.map(t => {
          const badgeClass = getStatusBadgeClass(t.status);
          const typeName = t.task_type === 'library_scrape'
            ? 'Library Scrape'
            : (t.task_type === 'metadata_ingest' ? 'Metadata Ingest' : t.task_type);

          const started = t.started_at ? formatTime(t.started_at) : '-';
          const completed = t.completed_at ? formatTime(t.completed_at) : (t.status === 'running' ? 'In Progress' : '-');
          const pct = Math.round(t.progress_pct || 0);

          return `
            <tr>
              <td class="font-mono"><strong>#${t.id}</strong></td>
              <td>${typeName}</td>
              <td><span class="status-badge ${badgeClass}">${(t.status || '').toUpperCase()}</span></td>
              <td class="font-mono">${pct}%</td>
              <td class="cell-label-truncate font-mono font-xs">
                ${escapeHtml(t.progress_label || '-')}
              </td>
              <td class="font-mono text-dim font-sm">${started}</td>
              <td class="font-mono text-dim font-sm">${completed}</td>
            </tr>
          `;
        }).join('');
      }
    }

    return active;
  } catch (err) {
    console.error('Failed to render tasks view:', err);
    return null;
  }
}

export async function openScraperModal() {
  const modal = document.getElementById('modal-scraper');
  if (modal) modal.style.display = 'flex';

  const statusEl = document.getElementById('scrape-modal-status');
  const summaryEl = document.getElementById('scrape-modal-summary');
  const progressEl = document.getElementById('scrape-modal-progress');
  const logEl = document.getElementById('scrape-modal-log');
  const triggerBtn = document.getElementById('btn-trigger-scrape');

  try {
    const data = await fetchTasksStatus();
    const active = data.active_task;

    if (active && active.task_type === 'library_scrape') {
      if (statusEl) {
        statusEl.textContent = 'RUNNING';
        statusEl.className = 'status-badge status-queued';
      }
      if (summaryEl) summaryEl.textContent = active.progress_label || 'Scraping library...';
      if (progressEl) progressEl.style.width = `${active.progress_pct || 0}%`;
      if (logEl) logEl.textContent = active.log_output || 'Running...';
      if (triggerBtn) {
        triggerBtn.disabled = true;
        triggerBtn.textContent = 'Scraper Running...';
      }
    } else {
      if (statusEl) {
        statusEl.textContent = 'READY';
        statusEl.className = 'status-badge status-not_downloaded';
      }
      if (summaryEl) summaryEl.textContent = 'Scraper idle. Click below to begin.';
      if (progressEl) progressEl.style.width = '0%';
      if (logEl) logEl.textContent = 'Ready for execution trigger.';
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.textContent = 'Start Library Scrape';
      }
    }
  } catch (err) {
    if (logEl) logEl.textContent = `Error reading initial scraper state: ${err.message}`;
  }
}

export async function triggerLibraryScrape() {
  const triggerBtn = document.getElementById('btn-trigger-scrape');
  const statusEl = document.getElementById('scrape-modal-status');
  const summaryEl = document.getElementById('scrape-modal-summary');
  const logEl = document.getElementById('scrape-modal-log');

  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.textContent = 'Launching...';
  }

  try {
    const res = await startScraper();
    if (statusEl) {
      statusEl.textContent = 'RUNNING';
      statusEl.className = 'status-badge status-queued';
    }
    if (summaryEl) summaryEl.textContent = `Task #${res.task_id} launched. Intercepting API payloads...`;
    if (logEl) logEl.textContent = `Scrape task #${res.task_id} started successfully.\n`;

    if (typeof window.checkHealth === 'function') {
      window.checkHealth();
    }
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = 'FAILED';
      statusEl.className = 'status-badge status-failed';
    }
    if (summaryEl) summaryEl.textContent = `Error: ${err.message}`;
    if (logEl) logEl.textContent += `\nLaunch failed: ${err.message}`;
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = 'Start Library Scrape';
    }
  }
}

export async function openIngestModal() {
  const modal = document.getElementById('modal-ingest');
  if (modal) modal.style.display = 'flex';
  await loadRecentScrapesForIngest();
}

export async function loadRecentScrapesForIngest() {
  const select = document.getElementById('ingest-scrape-folder-select');
  if (!select) return;

  select.innerHTML = '<option value="">Loading available scrape runs...</option>';

  try {
    const data = await fetchScrapesList();
    const scrapes = data.scrapes || [];

    if (scrapes.length === 0) {
      select.innerHTML = '<option value="">No scrape runs found on disk (run Scraper first)</option>';
      return;
    }

    select.innerHTML = scrapes.map(s => {
      const countLabel = s.model_count ? `${s.model_count} models` : 'empty';
      return `<option value="${s.folder}">${s.folder} (${countLabel})</option>`;
    }).join('');
  } catch (err) {
    select.innerHTML = `<option value="">Error loading scrapes: ${err.message}</option>`;
  }
}

export async function triggerMetadataIngest() {
  const triggerBtn = document.getElementById('btn-trigger-ingest');
  const folderBtn = document.getElementById('tab-btn-folder');
  const isFolderMode = folderBtn ? folderBtn.classList.contains('active') : true;

  let payload = {};
  if (isFolderMode) {
    const select = document.getElementById('ingest-scrape-folder-select');
    const folder = select ? select.value.trim() : '';
    if (!folder) {
      alert('Please select a scrape folder.');
      return;
    }
    payload.scrape_folder = folder;
  } else {
    const textarea = document.getElementById('ingest-raw-ids');
    const rawIds = textarea ? textarea.value.trim() : '';
    if (!rawIds) {
      alert('Please enter at least one numeric model ID.');
      return;
    }
    payload.raw_ids = rawIds;
  }

  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.textContent = 'Queueing Ingestion...';
  }

  try {
    const res = await startIngest(payload);

    if (res.new === 0) {
      renderIngestReport({
        total_submitted: res.submitted,
        skipped_already_in_db: res.skipped,
        ingested: 0,
        tombstoned: 0,
        failed: 0,
        creators: {}
      });
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.textContent = 'Database Up to Date';
      }
      return;
    }

    if (triggerBtn) {
      triggerBtn.textContent = `Task #${res.task_id} Ingesting...`;
    }

    if (typeof window.checkHealth === 'function') {
      window.checkHealth();
    }
  } catch (err) {
    alert(`Ingest start failed: ${err.message}`);
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = 'Start Metadata Ingest';
    }
  }
}

export function renderIngestReport(summary = {}) {
  const reportDisplay = document.getElementById('ingest-report-display');
  if (!reportDisplay) return;

  reportDisplay.style.display = 'block';

  const submittedEl = document.getElementById('report-submitted');
  const ingestedEl = document.getElementById('report-ingested');
  const tombstonedEl = document.getElementById('report-tombstoned');
  const skippedEl = document.getElementById('report-skipped');
  const failedEl = document.getElementById('report-failed');
  const creatorsContainer = document.getElementById('report-creators-container');

  if (submittedEl) submittedEl.textContent = summary.total_submitted ?? summary.submitted ?? 0;
  if (ingestedEl) ingestedEl.textContent = summary.ingested ?? 0;
  if (tombstonedEl) tombstonedEl.textContent = summary.tombstoned ?? summary.tombstoned_count ?? 0;
  if (skippedEl) skippedEl.textContent = summary.skipped_already_in_db ?? summary.skipped ?? 0;
  if (failedEl) failedEl.textContent = summary.failed ?? 0;

  if (creatorsContainer) {
    const creators = summary.creators || {};
    const creatorNames = Object.keys(creators);

    if (creatorNames.length === 0) {
      creatorsContainer.innerHTML = '<div class="text-dim font-xs mt-2">No new creators or models to breakdown.</div>';
    } else {
      creatorsContainer.innerHTML = `
        <div class="report-breakdown-wrap">
          <div class="report-breakdown-title">
            Creator Ingestion Breakdown
          </div>
          <table class="report-breakdown-table">
            <thead>
              <tr>
                <th>Creator / Studio</th>
                <th class="col-models-added">Models Added</th>
                <th class="col-presupported">Pre-Supported</th>
              </tr>
            </thead>
            <tbody>
              ${creatorNames.map(name => {
                const c = creators[name];
                return `
                  <tr>
                    <td class="cell-creator-accent">${escapeHtml(name)}</td>
                    <td class="font-mono">${c.count || 0}</td>
                    <td class="font-mono">${c.pre_supported || 0}</td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }
  }
}

export async function cancelActiveTask() {
  const cancelBtn = document.getElementById('btn-cancel-active-task');
  if (cancelBtn) {
    cancelBtn.disabled = true;
    cancelBtn.textContent = 'Cancelling...';
  }

  try {
    await cancelTask();
    await renderTasksView();
  } catch (err) {
    alert(`Cancellation request failed: ${err.message}`);
  } finally {
    if (cancelBtn) {
      cancelBtn.disabled = false;
      cancelBtn.textContent = 'Cancel Task';
    }
  }
}

export async function killActiveTask() {
  if (!confirm('Force-terminate running or stuck task? This will immediately release the task pipeline lock.')) return;
  const killBtn = document.getElementById('btn-kill-active-task');
  if (killBtn) {
    killBtn.disabled = true;
    killBtn.textContent = 'Killing...';
  }

  try {
    await killTask();
    await renderTasksView();
  } catch (err) {
    alert(`Kill request failed: ${err.message}`);
  } finally {
    if (killBtn) {
      killBtn.disabled = false;
      killBtn.textContent = 'Force Kill';
    }
  }
}

export async function clearTaskHistory() {
  if (!confirm('Clear all completed, failed, and cancelled pipeline task history?')) return;
  try {
    await clearTasksHistory();
    await renderTasksView();
  } catch (err) {
    alert(`Clear history failed: ${err.message}`);
  }
}

function getStatusBadgeClass(status) {
  switch ((status || '').toLowerCase()) {
    case 'completed':
      return 'status-completed';
    case 'running':
      return 'status-queued';
    case 'failed':
      return 'status-failed';
    case 'cancelled':
      return 'status-partial';
    default:
      return 'status-not_downloaded';
  }
}

function formatTime(isoStr) {
  if (!isoStr) return '-';
  try {
    const parts = isoStr.replace('T', ' ').split('.')[0].split(' ');
    return parts.length > 1 ? `${parts[0]} ${parts[1]}` : isoStr;
  } catch {
    return isoStr;
  }
}
