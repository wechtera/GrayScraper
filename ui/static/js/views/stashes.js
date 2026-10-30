/**
 * ui/static/js/views/stashes.js - Stash System ("A pile of gray") view controller.
 * Handles sidebar listing, active stash toggling, pre-flight capacity audits,
 * inline creation, item manifest management, and background slicer print queue exporting.
 */

import {
  fetchStashes,
  fetchActiveStash,
  fetchStashDetail,
  createStash,
  setActiveStash,
  deleteStash,
  removeStashItem,
  exportStash
} from '../api.js';
import { formatBytes, escapeHtml } from '../utils.js';

let currentSelectedStashId = null;
let currentStashesList = [];

export function updateHeaderStashWidget(activeStash) {
  const widget = document.getElementById('header-stash-widget');
  const nameEl = document.getElementById('header-stash-name');
  const countEl = document.getElementById('header-stash-count');
  if (!widget || !nameEl || !countEl) return;

  if (activeStash && activeStash.id) {
    widget.classList.add('has-active');
    nameEl.textContent = activeStash.name || 'Active Stash';
    countEl.textContent = (activeStash.item_count || 0).toLocaleString();
    widget.title = `Active Stash: ${activeStash.name} (${formatBytes(activeStash.total_bytes || 0)})`;
  } else {
    widget.classList.remove('has-active');
    nameEl.textContent = 'No Active Stash';
    countEl.textContent = '0';
    widget.title = 'No active stash selected. Click to manage stashes.';
  }
}

export async function loadStashesView() {
  const sidebarContainer = document.getElementById('stashes-sidebar-list');
  if (sidebarContainer) {
    sidebarContainer.innerHTML = '<div class="table-msg">Loading stashes...</div>';
  }

  try {
    const [stashesData, activeData] = await Promise.all([
      fetchStashes(),
      fetchActiveStash().catch(() => ({ active_stash: null }))
    ]);

    currentStashesList = stashesData.stashes || [];
    const activeStash = activeData.active_stash;

    updateHeaderStashWidget(activeStash);

    if (!currentSelectedStashId || !currentStashesList.some(s => s.id === currentSelectedStashId)) {
      if (activeStash && activeStash.id) {
        currentSelectedStashId = activeStash.id;
      } else if (currentStashesList.length > 0) {
        currentSelectedStashId = currentStashesList[0].id;
      } else {
        currentSelectedStashId = null;
      }
    }

    renderStashesSidebar();

    if (currentSelectedStashId) {
      await loadStashDetail(currentSelectedStashId);
    } else {
      renderEmptyWorkspace();
    }
  } catch (err) {
    console.error('Failed to load stashes view:', err);
    if (sidebarContainer) {
      sidebarContainer.innerHTML = `<div class="browse-item-alert" style="padding:16px;">Failed to load stashes: ${escapeHtml(err.message)}</div>`;
    }
  }
}

function renderStashesSidebar() {
  const container = document.getElementById('stashes-sidebar-list');
  if (!container) return;

  if (currentStashesList.length === 0) {
    container.innerHTML = `
      <div class="stash-sidebar-empty">
        <p class="text-dim font-xs">No stashes created yet.</p>
        <p class="text-dim font-xs mt-1">Create one below to begin organizing your pile of gray.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = currentStashesList.map(stash => {
    const isSelected = stash.id === currentSelectedStashId;
    const isActive = Boolean(stash.is_active);

    return `
      <div class="stash-nav-card ${isSelected ? 'selected' : ''} ${isActive ? 'is-active' : ''}" 
           onclick="window.selectStash(${stash.id})">
        <div class="stash-nav-top">
          <span class="stash-nav-title font-semibold text-main">${escapeHtml(stash.name)}</span>
          ${isActive ? '<span class="status-badge status-completed font-xs">ACTIVE</span>' : ''}
        </div>
        <div class="stash-nav-meta text-dim font-xs mt-1">
          <span>${(stash.item_count || 0).toLocaleString()} items</span>
          <span>&bull;</span>
          <span>${formatBytes(stash.total_bytes || 0)}</span>
        </div>
        <div class="stash-nav-desc text-dim font-xs mt-1" style="font-style:italic;">
          ${escapeHtml(stash.description || 'A pile of gray')}
        </div>
      </div>
    `;
  }).join('');
}

export async function selectStash(stashId) {
  currentSelectedStashId = stashId;
  renderStashesSidebar();
  await loadStashDetail(stashId);
}

export async function loadStashDetail(stashId) {
  const container = document.getElementById('stash-detail-workspace');
  if (!container) return;

  container.innerHTML = '<div class="loading-placeholder">Resolving stash items and pre-flight sizes...</div>';

  try {
    const res = await fetchStashDetail(stashId);
    const stash = res.stash;
    if (!stash) {
      renderEmptyWorkspace();
      return;
    }

    const items = stash.items || [];

    const activeToggleBtn = stash.is_active
      ? '<span class="status-badge status-completed" style="font-size:11px; padding:4px 8px;">CURRENT ACTIVE STASH</span>'
      : `<button type="button" class="btn-pill btn-action-primary" onclick="window.handleSetActiveStash(${stash.id})">Set as Active Stash</button>`;

    container.innerHTML = `
      <div class="stash-workspace-header">
        <div class="stash-header-info">
          <div class="flex items-center gap-2">
            <h2 class="stash-title-accent text-main font-bold font-lg">${escapeHtml(stash.name)}</h2>
            ${stash.is_active ? '<span class="status-badge status-completed font-xs">ACTIVE QUICK-ADD</span>' : ''}
          </div>
          <div class="text-dim font-xs mt-1">${escapeHtml(stash.description || 'A pile of gray')}</div>
        </div>
        <div class="stash-header-actions">
          <button type="button" 
                  class="btn-pill btn-action-primary" 
                  id="btn-export-stash" 
                  onclick="window.handleExportStash(${stash.id})">
            Prepare for Slicer
          </button>
          ${activeToggleBtn}
          <button type="button" class="btn-pill btn-action-cancel" onclick="window.handleDeleteStash(${stash.id}, '${escapeHtml(stash.name)}')">
            Delete Stash
          </button>
        </div>
      </div>

      <!-- Pre-Flight Manifest Metrics -->
      <div class="metrics-grid metrics-grid-reconcile" style="margin: 0 0 20px 0;">
        <div class="stat-card">
          <div class="stat-label">Total Line Items</div>
          <div class="stat-value font-mono">${(stash.total_items || 0).toLocaleString()}</div>
          <div class="stat-sub text-dim font-xs">${stash.models_count || 0} Models &bull; ${stash.files_count || 0} Files</div>
        </div>
        <div class="stat-card">
          <div class="stat-label stat-label-accent">Total Volume</div>
          <div class="stat-value stat-value-accent font-mono">${formatBytes(stash.total_bytes || 0)}</div>
          <div class="stat-sub text-dim font-xs">Archive Transfer Size</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Submodels / Figures</div>
          <div class="stat-value font-mono">${(stash.submodels_count || 0).toLocaleString()}</div>
          <div class="stat-sub text-dim font-xs">Segmented Miniatures</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Archive Members</div>
          <div class="stat-value font-mono">${(stash.archive_members_count || 0).toLocaleString()}</div>
          <div class="stat-sub text-dim font-xs">Raw Internal Meshes</div>
        </div>
      </div>

      <!-- Staged Items Section -->
      <div class="section-header-row" style="margin: 0 0 14px 0;">
        <h3 class="section-header-title">Staged Manifest Items (${items.length})</h3>
        <div class="text-dim font-xs">Manifest operations only &bull; Original files are preserved</div>
      </div>

      ${items.length === 0 ? `
        <div class="results-placeholder" style="margin: 0;">
          This stash is empty. Add models, individual files, or submodels from the catalog to build this manifest.
        </div>
      ` : `
        <div class="grid stash-items-grid" style="margin: 0;">
          ${items.map(item => renderStashItemCard(item)).join('')}
        </div>
      `}
    `;
  } catch (err) {
    container.innerHTML = `<div class="browse-item-alert" style="padding:20px;">Failed loading stash details: ${escapeHtml(err.message)}</div>`;
  }
}

/**
 * Triggers the background slicer export pipeline and monitors task progress.
 */
export async function handleExportStash(stashId) {
  const exportBtn = document.getElementById('btn-export-stash');
  if (exportBtn) {
    exportBtn.disabled = true;
    exportBtn.innerHTML = `
      <span class="spinner-inline" style="display:inline-block; width:12px; height:12px; border:2px solid #fff; border-top-color:transparent; border-radius:50%; animation:spin 0.6s linear infinite; vertical-align:middle; margin-right:6px;"></span>
      Dispatching Background Task...
    `;
  }

  try {
    const res = await exportStash(stashId, { filter: 'all' });
    if (res.task_id) {
      showExportProgressModal(res.task_id, res.stash_name);
      pollExportTaskProgress(res.task_id);
    } else if (res.export_path) {
      showExportSuccessModal(res);
    }
  } catch (err) {
    console.error('Stash export error:', err);
    alert(`Failed to start slicer export:\n\n${err.message}`);
  } finally {
    if (exportBtn) {
      exportBtn.disabled = false;
      exportBtn.innerHTML = 'Prepare for Slicer';
    }
  }
}

function showExportProgressModal(taskId, stashName) {
  const existing = document.getElementById('stash-export-progress-modal');
  if (existing) existing.remove();

  const modalHtml = `
    <div id="stash-export-progress-modal" class="modal-backdrop" style="position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.75); display:flex; align-items:center; justify-content:center; z-index:9999;">
      <div class="modal-card" style="background:var(--bg-card, #1e1e24); border:1px solid var(--border-color, #333); border-radius:8px; max-width:540px; width:90%; padding:24px; box-shadow:0 12px 32px rgba(0,0,0,0.5);">
        <div class="flex items-center justify-between mb-2">
          <h3 class="text-main font-bold font-lg" style="margin:0;">Extracting to Local Print Queue</h3>
          <span class="status-badge status-pending font-xs">TASK #${taskId}</span>
        </div>
        <p class="text-dim font-sm mb-3">
          Surgically extracting curated items for <strong>${escapeHtml(stashName || 'Stash')}</strong> to local scratch space...
        </p>

        <!-- Progress bar container -->
        <div style="background:var(--bg-main, #141418); border-radius:4px; height:12px; overflow:hidden; margin-bottom:8px; border:1px solid var(--border-color, #2b2b36);">
          <div id="export-task-progress-bar" style="background:var(--accent, #6366f1); height:100%; width:5%; transition:width 0.3s ease;"></div>
        </div>

        <div class="flex justify-between font-mono font-xs text-dim mb-3">
          <span id="export-task-status-label">Initializing background worker...</span>
          <span id="export-task-pct">5%</span>
        </div>

        <div class="flex justify-end gap-2">
          <button type="button" class="btn-pill btn-action-cancel" onclick="window.cancelExportTask(${taskId})">
            Cancel Export
          </button>
        </div>
      </div>
    </div>
  `;
  document.body.insertAdjacentHTML('beforeend', modalHtml);
}

async function pollExportTaskProgress(taskId) {
  const modal = document.getElementById('stash-export-progress-modal');
  if (!modal) return;

  try {
    const res = await fetch('/api/tasks/status');
    const data = await res.json();
    const active = data.active_task;
    const recent = data.recent_tasks || [];

    let targetTask = null;
    if (active && active.id === taskId) {
      targetTask = active;
    } else {
      targetTask = recent.find(t => t.id === taskId);
    }

    if (!targetTask) {
      setTimeout(() => pollExportTaskProgress(taskId), 1000);
      return;
    }

    const bar = document.getElementById('export-task-progress-bar');
    const label = document.getElementById('export-task-status-label');
    const pct = document.getElementById('export-task-pct');

    if (bar && pct && label) {
      const p = Math.max(5, targetTask.progress_pct || 0);
      bar.style.width = `${p}%`;
      pct.textContent = `${Math.round(p)}%`;
      if (targetTask.progress_label) {
        label.textContent = targetTask.progress_label;
      }
    }

    if (targetTask.status === 'completed') {
      closeExportProgressModal();
      const exportSummary = targetTask.result_summary || targetTask.artifacts || {};
      showExportSuccessModal(exportSummary);
      return;
    }

    if (targetTask.status === 'failed') {
      closeExportProgressModal();
      alert(`Stash export task failed:\n\n${targetTask.error_message || targetTask.progress_label || 'Extraction error'}`);
      return;
    }

    if (targetTask.status === 'cancelled') {
      closeExportProgressModal();
      alert('Stash export was cancelled.');
      return;
    }

    setTimeout(() => pollExportTaskProgress(taskId), 800);
  } catch (err) {
    console.warn('Error polling export task:', err);
    setTimeout(() => pollExportTaskProgress(taskId), 1500);
  }
}

export function closeExportProgressModal() {
  const modal = document.getElementById('stash-export-progress-modal');
  if (modal) modal.remove();
}

export async function cancelExportTask(taskId) {
  try {
    await fetch('/api/tasks/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId })
    });
    closeExportProgressModal();
  } catch (err) {
    alert(`Failed to cancel task: ${err.message}`);
  }
}

function showExportSuccessModal(exportData) {
  const existingModal = document.getElementById('stash-export-success-modal');
  if (existingModal) existingModal.remove();

  const exportPath = exportData.export_path || '';
  const totalFiles = exportData.total_files || 0;
  const totalBytes = exportData.total_bytes || 0;

  const modalHtml = `
    <div id="stash-export-success-modal" class="modal-backdrop" style="position:fixed; top:0; left:0; width:100vw; height:100vh; background:rgba(0,0,0,0.75); display:flex; align-items:center; justify-content:center; z-index:9999;">
      <div class="modal-card" style="background:var(--bg-card, #1e1e24); border:1px solid var(--border-color, #333); border-radius:8px; max-width:640px; width:90%; padding:24px; box-shadow:0 12px 32px rgba(0,0,0,0.5);">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-main font-bold font-lg" style="margin:0; display:flex; align-items:center; gap:8px;">
            <span style="color:#10b981;">&#10004;</span> Slicer Print Queue Ready
          </h3>
          <button type="button" class="btn-icon" onclick="window.closeExportSuccessModal()" style="background:transparent; border:none; color:var(--text-dim, #888); font-size:18px; cursor:pointer;">&times;</button>
        </div>

        <p class="text-dim font-sm mb-3">
          Assets for <strong>${escapeHtml(exportData.stash_name || 'Stash')}</strong> have been surgically unpacked from the vault into your local print directory.
        </p>

        <div style="background:var(--bg-main, #141418); border:1px solid var(--border-color, #2b2b36); border-radius:6px; padding:12px; margin-bottom:16px;">
          <div class="text-dim font-xs mb-1 font-semibold">DESTINATION DIRECTORY</div>
          <div class="font-mono text-main font-xs" style="word-break:break-all; user-select:all;" id="export-path-text">${escapeHtml(exportPath)}</div>
        </div>

        <div class="metrics-grid" style="grid-template-columns: 1fr 1fr; margin-bottom:20px;">
          <div class="stat-card" style="padding:10px 14px;">
            <div class="stat-label">Materialized Files</div>
            <div class="stat-value font-mono" style="font-size:18px;">${totalFiles.toLocaleString()}</div>
          </div>
          <div class="stat-card" style="padding:10px 14px;">
            <div class="stat-label">Unpacked Footprint</div>
            <div class="stat-value font-mono" style="font-size:18px;">${formatBytes(totalBytes)}</div>
          </div>
        </div>

        <div class="flex justify-end gap-2">
          <button type="button" class="btn-pill" id="btn-copy-export-path" onclick="window.copyExportPath('${escapeHtml(exportPath.replace(/\\/g, '\\\\'))}')">
            Copy Path
          </button>
          <button type="button" class="btn-pill btn-action-primary" onclick="window.closeExportSuccessModal()">
            Done
          </button>
        </div>
      </div>
    </div>
  `;

  document.body.insertAdjacentHTML('beforeend', modalHtml);
}

export function closeExportSuccessModal() {
  const modal = document.getElementById('stash-export-success-modal');
  if (modal) modal.remove();
}

export async function copyExportPath(textToCopy) {
  const btn = document.getElementById('btn-copy-export-path');
  try {
    await navigator.clipboard.writeText(textToCopy);
    if (btn) {
      const original = btn.textContent;
      btn.textContent = 'Copied!';
      btn.style.borderColor = '#10b981';
      setTimeout(() => {
        btn.textContent = original;
        btn.style.borderColor = '';
      }, 2000);
    }
  } catch (err) {
    const textEl = document.getElementById('export-path-text');
    if (textEl) {
      const range = document.createRange();
      range.selectNodeContents(textEl);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }
}

function renderStashItemCard(item) {
  const typeBadgeClass = {
    model: 'badge-bundle',
    file: 'badge-month',
    submodel: 'badge-bonus',
    archive_member: 'badge-ext'
  }[item.item_type] || 'badge-month';

  const typeLabel = {
    model: 'MODEL BUNDLE',
    file: 'ARCHIVE FILE',
    submodel: 'SUBMODEL',
    archive_member: 'RAW MESH'
  }[item.item_type] || item.item_type.toUpperCase();

  const thumbHtml = item.thumbnail_url
    ? `<img src="${item.thumbnail_url}" alt="${escapeHtml(item.title)}" loading="lazy">`
    : `<div class="text-dim font-xs font-mono" style="padding:20px; text-align:center;">NO PREVIEW</div>`;

  return `
    <div class="card stash-item-card">
      <div class="thumb-wrap">
        ${thumbHtml}
        <span class="badge ${typeBadgeClass} stash-type-pill">${typeLabel}</span>
      </div>
      <div class="card-body">
        <div class="card-title cell-clickable-title" onclick="window.openModelDetailModal(${item.model_id})">
          ${escapeHtml(item.title)}
        </div>
        <div class="creator-name">${escapeHtml(item.creator_name)}</div>

        ${item.relative_path ? `
          <div class="text-dim font-xs font-mono" style="word-break:break-all;">
            ${escapeHtml(item.relative_path)}
          </div>
        ` : ''}

        <div class="card-badge-row mt-1">
          <span class="badge badge-ext">${escapeHtml(item.file_extension || 'FILE')}</span>
          ${item.is_supported ? '<span class="status-badge status-completed font-xs">PRE-SUPPORTED</span>' : ''}
        </div>

        <div class="footer mt-auto">
          <span class="font-mono text-main font-semibold">${formatBytes(item.size_bytes || 0)}</span>
          <div class="footer-actions">
            <button type="button" class="btn-pill font-xs" onclick="window.openModelDetailModal(${item.model_id})">
              Details
            </button>
            <button type="button" class="btn-pill btn-action-cancel font-xs" onclick="window.handleRemoveStashItem(${item.id})">
              Remove
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderEmptyWorkspace() {
  const container = document.getElementById('stash-detail-workspace');
  if (!container) return;

  container.innerHTML = `
    <div class="results-placeholder" style="margin: 0; padding: 48px 24px;">
      <h3 class="text-main font-bold font-lg mb-2">No Stash Selected</h3>
      <p class="text-dim font-sm">Create a new stash in the sidebar or select an existing stash manifest to review items.</p>
    </div>
  `;
}

export async function handleCreateStash() {
  const nameInput = document.getElementById('new-stash-name');
  const makeActiveCheckbox = document.getElementById('new-stash-make-active');

  const name = nameInput ? nameInput.value.trim() : '';
  const makeActive = makeActiveCheckbox ? makeActiveCheckbox.checked : false;

  if (!name) {
    alert('Please enter a name for the new stash.');
    return;
  }

  try {
    const res = await createStash({
      name,
      description: 'A pile of gray',
      make_active: makeActive
    });

    if (nameInput) nameInput.value = '';
    if (makeActiveCheckbox) makeActiveCheckbox.checked = false;

    currentSelectedStashId = res.stash.id;
    await loadStashesView();
  } catch (err) {
    alert(`Failed to create stash: ${err.message}`);
  }
}

export async function handleSetActiveStash(stashId) {
  try {
    const res = await setActiveStash(stashId);
    updateHeaderStashWidget(res.active_stash);
    await loadStashesView();
  } catch (err) {
    alert(`Failed to set active stash: ${err.message}`);
  }
}

export async function handleDeleteStash(stashId, stashName) {
  if (!confirm(`Delete stash manifest "${stashName}"?\n\nThis will only delete the logical staging manifest. Downloaded models and original library files on disk will NEVER be touched.`)) {
    return;
  }

  try {
    await deleteStash(stashId);
    if (currentSelectedStashId === stashId) {
      currentSelectedStashId = null;
    }
    await loadStashesView();
  } catch (err) {
    alert(`Failed to delete stash: ${err.message}`);
  }
}

export async function handleRemoveStashItem(itemId) {
  try {
    await removeStashItem(itemId);
    if (currentSelectedStashId) {
      await loadStashDetail(currentSelectedStashId);
      const stashesData = await fetchStashes();
      currentStashesList = stashesData.stashes || [];
      renderStashesSidebar();
      const activeRes = await fetchActiveStash().catch(() => ({ active_stash: null }));
      updateHeaderStashWidget(activeRes.active_stash);
    }
  } catch (err) {
    alert(`Failed to remove item: ${err.message}`);
  }
}

Object.assign(window, {
  selectStash,
  handleSetActiveStash,
  handleDeleteStash,
  handleRemoveStashItem,
  handleCreateStash,
  handleExportStash,
  cancelExportTask,
  closeExportSuccessModal,
  closeExportProgressModal,
  copyExportPath,
});
