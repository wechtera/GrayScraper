/**
 * ui/static/js/views/stashes.js - Stash System ("A pile of gray") view controller.
 * Handles sidebar listing, active stash toggling, pre-flight capacity audits,
 * inline creation, and item manifest management.
 */

import {
  fetchStashes,
  fetchActiveStash,
  fetchStashDetail,
  createStash,
  setActiveStash,
  deleteStash,
  removeStashItem
} from '../api.js';
import { formatBytes, escapeHtml } from '../utils.js';

let currentSelectedStashId = null;
let currentStashesList = [];

/**
 * Updates the persistent top-nav stash button in the application header.
 */
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

/**
 * Main entry point called when navigating to the Stashes tab.
 */
export async function loadStashesView() {
  const sidebarContainer = document.getElementById('stashes-sidebar-list');
  const workspaceContainer = document.getElementById('stash-detail-workspace');
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

    // If currently selected stash is invalid, default to active stash or first stash
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

/**
 * Renders the left sidebar list of available stash manifests.
 */
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

/**
 * Selects a stash from the sidebar and mounts it into the right workspace.
 */
export async function selectStash(stashId) {
  currentSelectedStashId = stashId;
  renderStashesSidebar();
  await loadStashDetail(stashId);
}

/**
 * Fetches detail and renders the right workspace for a specific stash.
 */
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
 * Renders an individual card within the stash items grid.
 */
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

/**
 * Creates a new stash manifest from the sidebar form.
 */
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

/**
 * Sets the active stash manifest.
 */
export async function handleSetActiveStash(stashId) {
  try {
    const res = await setActiveStash(stashId);
    updateHeaderStashWidget(res.active_stash);
    await loadStashesView();
  } catch (err) {
    alert(`Failed to set active stash: ${err.message}`);
  }
}

/**
 * Deletes a stash manifest (manifest-only deletion; original files untouched).
 */
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

/**
 * Removes an individual line item from the currently opened stash manifest.
 */
export async function handleRemoveStashItem(itemId) {
  try {
    await removeStashItem(itemId);
    if (currentSelectedStashId) {
      await loadStashDetail(currentSelectedStashId);
      // Refresh sidebar metrics
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
