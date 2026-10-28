/**
 * ui/static/js/views/stash_modal.js - Universal Add-to-Stash Modal Controller.
 * Supports all 4 polymorphic tiers (model, file, submodel, archive_member) + bulk models.
 * Stacks cleanly at z-index: 1200 over any active modal.
 */

import {
  fetchStashes,
  fetchActiveStash,
  addStashItem,
  bulkAddStashModels,
  createStash,
  setActiveStash
} from '../api.js';
import { updateHeaderStashWidget } from './stashes.js';
import { escapeHtml } from '../utils.js';

let currentItemContext = null;
let onStashSuccessCallback = null;

/**
 * Main entry point: opens the Add-to-Stash modal.
 * @param {Object} itemContext
 *   - item_type: 'model' | 'file' | 'submodel' | 'archive_member' | 'bulk_models'
 *   - model_id: number (or model_ids: number[] for bulk)
 *   - file_id: optional number
 *   - submodel_id: optional number
 *   - archive_member_id: optional number
 *   - title: string (display name)
 *   - creator: optional string
 * @param {Function} [onSuccess] Optional callback executed after successful addition
 */
export async function openAddToStashModal(itemContext, onSuccess = null) {
  currentItemContext = itemContext;
  onStashSuccessCallback = onSuccess;

  ensureModalContainer();
  const backdrop = document.getElementById('modal-add-to-stash-backdrop');
  const body = document.getElementById('stash-modal-dynamic-body');
  const titleEl = document.getElementById('stash-modal-item-title');
  const badgeEl = document.getElementById('stash-modal-tier-badge');

  backdrop.style.display = 'flex';
  body.innerHTML = '<div class="loading-placeholder">Checking stash manifests...</div>';

  const tierLabels = {
    model: 'MODEL BUNDLE',
    file: 'ARCHIVE FILE',
    submodel: 'LOGICAL SUBMODEL',
    archive_member: 'RAW MESH MEMBER',
    bulk_models: 'BULK MODELS'
  };

  if (titleEl) titleEl.textContent = itemContext.title || `Model #${itemContext.model_id}`;
  if (badgeEl) badgeEl.textContent = tierLabels[itemContext.item_type] || 'ITEM';

  try {
    const [stashesData, activeData] = await Promise.all([
      fetchStashes(),
      fetchActiveStash().catch(() => ({ active_stash: null }))
    ]);

    const stashes = stashesData.stashes || [];
    const activeStash = activeData.active_stash;

    if (activeStash) {
      updateHeaderStashWidget(activeStash);
    }

    // --- ZERO-STASH GATE ---
    if (stashes.length === 0) {
      renderZeroStashGate(body);
      return;
    }

    // --- RENDER 3-SECTION STASH SELECTION FORM ---
    renderStashSelectionForm(body, stashes, activeStash);
  } catch (err) {
    body.innerHTML = `<div class="browse-item-alert" style="padding:16px;">Failed loading stashes: ${escapeHtml(err.message)}</div>`;
  }
}

/**
 * Renders the Zero-Stash Gate informative view with CTA.
 */
function renderZeroStashGate(container) {
  container.innerHTML = `
    <div class="stash-modal-zero-gate">
      <div class="stash-zero-icon">&boxbox;</div>
      <h3 class="text-main font-bold font-lg mb-2">No Stash Manifests Found</h3>
      <p class="text-dim font-sm mb-4" style="max-width: 400px; margin: 0 auto 16px auto; line-height: 1.5;">
        You haven't created any stash manifests yet. Stashes allow you to stage and curate 3D models, components, and raw bits into organized print batches (&ldquo;A pile of gray&rdquo;).
      </p>
      <button type="button" class="btn-pill btn-action-primary" onclick="window.goToStashManager()">
        Go to Stash Manager &rarr;
      </button>
    </div>
  `;
}

/**
 * Renders the 3 selection/action sections when stashes exist.
 */
function renderStashSelectionForm(container, stashes, activeStash) {
  const hasActive = Boolean(activeStash && activeStash.id);

  container.innerHTML = `
    <div id="stash-modal-feedback" class="stash-modal-feedback" style="display:none;"></div>

    <!-- Section 1: Quick Add to Active Stash -->
    <div class="stash-modal-section">
      <div class="stash-modal-section-title">1. Quick Add to Active Stash</div>
      ${hasActive ? `
        <div class="stash-quick-add-box">
          <div>
            <div class="font-semibold text-main">${escapeHtml(activeStash.name)}</div>
            <div class="text-dim font-xs mt-1">Currently holds ${(activeStash.item_count || 0).toLocaleString()} item${activeStash.item_count === 1 ? '' : 's'}</div>
          </div>
          <button type="button" class="btn-pill btn-action-primary" id="btn-quick-add-stash" onclick="window.executeAddToStash(${activeStash.id}, '${escapeHtml(activeStash.name).replace(/'/g, "\\'")}')">
            Quick Add to ${escapeHtml(activeStash.name)}
          </button>
        </div>
      ` : `
        <div class="text-dim font-xs" style="padding: 10px; background: rgba(0,0,0,0.2);">
          No active stash currently designated. Select one below or set an active stash.
        </div>
      `}
    </div>

    <!-- Section 2: Pick Existing Stash -->
    <div class="stash-modal-section">
      <div class="stash-modal-section-title">2. Choose Stash Manifest</div>
      <div class="stash-picker-row">
        <select id="stash-modal-select" class="search-box" style="flex:1;">
          ${stashes.map(s => `
            <option value="${s.id}" ${activeStash && s.id === activeStash.id ? 'selected' : ''}>
              ${escapeHtml(s.name)} (${s.item_count || 0} items)${s.is_active ? ' [Active]' : ''}
            </option>
          `).join('')}
        </select>
        <button type="button" class="btn-pill btn-action-queue" onclick="window.handleDropdownAddToStash()">
          Add to Stash
        </button>
      </div>
      <label class="control-checkbox-label font-xs mt-2">
        <input type="checkbox" id="stash-modal-make-active-chk"> Make this my active stash
      </label>
    </div>

    <!-- Section 3: Inline Creation -->
    <div class="stash-modal-section">
      <div class="stash-modal-section-title">3. Create New Stash &amp; Add</div>
      <input type="text" id="stash-modal-new-name" class="search-box select-full mb-2" placeholder="New stash name (e.g. October Resin Run)..." spellcheck="false">
      <div class="flex gap-2 justify-end">
        <button type="button" class="btn-pill" onclick="window.handleInlineCreateAndAdd(false)">
          Create &amp; Add
        </button>
        <button type="button" class="btn-pill btn-action-primary" onclick="window.handleInlineCreateAndAdd(true)">
          Create, Make Current &amp; Add
        </button>
      </div>
    </div>
  `;
}

/**
 * Handles adding to the chosen dropdown stash, optionally setting it active.
 */
export async function handleDropdownAddToStash() {
  const select = document.getElementById('stash-modal-select');
  const makeActiveChk = document.getElementById('stash-modal-make-active-chk');
  if (!select) return;

  const stashId = parseInt(select.value, 10);
  const stashName = select.options[select.selectedIndex]?.text || `Stash #${stashId}`;
  const makeActive = makeActiveChk ? makeActiveChk.checked : false;

  if (makeActive) {
    try {
      await setActiveStash(stashId);
    } catch (_) {}
  }

  await executeAddToStash(stashId, stashName);
}

/**
 * Handles inline stash creation followed by immediate item insertion.
 */
export async function handleInlineCreateAndAdd(makeActive = false) {
  const input = document.getElementById('stash-modal-new-name');
  const name = input ? input.value.trim() : '';
  if (!name) {
    showFeedback('Please enter a name for the new stash.', 'alert');
    return;
  }

  try {
    const res = await createStash({
      name,
      description: 'A pile of gray',
      make_active: makeActive
    });
    const newStash = res.stash;
    await executeAddToStash(newStash.id, newStash.name);
  } catch (err) {
    showFeedback(`Failed creating stash: ${err.message}`, 'alert');
  }
}

/**
 * Executes polymorphic insertion into the target stash.
 * Smoothly captures 409 duplicates with friendly notice.
 */
export async function executeAddToStash(stashId, stashName) {
  if (!currentItemContext) return;

  setButtonsDisabled(true);

  try {
    // Bulk Model insertion branch (from Downloads view)
    if (currentItemContext.item_type === 'bulk_models') {
      const modelIds = currentItemContext.model_ids || [];
      const res = await bulkAddStashModels(stashId, modelIds);

      let msg = `Added ${res.added} model${res.added === 1 ? '' : 's'} to "${escapeHtml(stashName)}".`;
      if (res.duplicates > 0) {
        msg += ` (${res.duplicates} already pending paint in stash).`;
      }
      showFeedback(msg, res.duplicates > 0 && res.added === 0 ? 'warn' : 'success');

      syncActiveWidget();
      if (onStashSuccessCallback) onStashSuccessCallback(res);
      setTimeout(closeAddToStashModal, 1200);
      return;
    }

    // Single Polymorphic item insertion branch
    const payload = {
      item_type: currentItemContext.item_type,
      model_id: currentItemContext.model_id,
      file_id: currentItemContext.file_id || null,
      submodel_id: currentItemContext.submodel_id || null,
      archive_member_id: currentItemContext.archive_member_id || null,
      notes: ''
    };

    const res = await addStashItem(stashId, payload);

    if (res.duplicate || res.already_exists) {
      showFeedback(`Already pending paint in &lsquo;${escapeHtml(res.stash_name || stashName)}&rsquo;.`, 'warn');
      setButtonsDisabled(false);
      return;
    }

    showFeedback(`Added to &lsquo;${escapeHtml(stashName)}&rsquo;!`, 'success');
    syncActiveWidget();
    if (onStashSuccessCallback) onStashSuccessCallback(res);
    setTimeout(closeAddToStashModal, 800);

  } catch (err) {
    showFeedback(`Error: ${err.message}`, 'alert');
    setButtonsDisabled(false);
  }
}

/**
 * Navigates directly from the Zero-Stash gate to the Stash Manager tab.
 */
export function goToStashManager() {
  closeAddToStashModal();
  if (typeof window.switchTab === 'function') {
    window.switchTab('stashes');
  } else {
    window.location.hash = '#view-stashes';
  }
}

/**
 * Closes the Add-to-Stash modal.
 */
export function closeAddToStashModal() {
  const backdrop = document.getElementById('modal-add-to-stash-backdrop');
  if (backdrop) backdrop.style.display = 'none';
  currentItemContext = null;
  onStashSuccessCallback = null;
}

function showFeedback(msg, type = 'success') {
  const el = document.getElementById('stash-modal-feedback');
  if (!el) return;
  el.className = `stash-modal-feedback feedback-${type}`;
  el.innerHTML = msg;
  el.style.display = 'block';
}

function setButtonsDisabled(disabled) {
  const modal = document.getElementById('modal-add-to-stash-backdrop');
  if (!modal) return;
  modal.querySelectorAll('button:not(.btn-modal-close)').forEach(b => {
    b.disabled = disabled;
  });
}

async function syncActiveWidget() {
  try {
    const activeData = await fetchActiveStash();
    updateHeaderStashWidget(activeData.active_stash);
  } catch (_) {}
}

/**
 * Ensures the modal container element exists in the DOM.
 */
function ensureModalContainer() {
  if (document.getElementById('modal-add-to-stash-backdrop')) return;

  const div = document.createElement('div');
  div.id = 'modal-add-to-stash-backdrop';
  div.className = 'modal-backdrop stash-modal-backdrop';
  div.innerHTML = `
    <div class="modal modal-md stash-modal-content">
      <div class="modal-header">
        <div>
          <div class="flex items-center gap-2">
            <h2 class="modal-title-accent">Add to Stash</h2>
            <span class="badge badge-bundle font-xs" id="stash-modal-tier-badge">ITEM</span>
          </div>
          <div class="modal-subtitle" id="stash-modal-item-title">Loading item...</div>
        </div>
        <button type="button" class="tab-btn btn-modal-close" onclick="window.closeAddToStashModal()">Close</button>
      </div>
      <div class="modal-body modal-body-stack" id="stash-modal-dynamic-body"></div>
    </div>
  `;
  document.body.appendChild(div);
}

// Bind methods to window for inline onclick accessibility
window.openAddToStashModal = openAddToStashModal;
window.closeAddToStashModal = closeAddToStashModal;
window.goToStashManager = goToStashManager;
window.handleDropdownAddToStash = handleDropdownAddToStash;
window.handleInlineCreateAndAdd = handleInlineCreateAndAdd;
window.executeAddToStash = executeAddToStash;
