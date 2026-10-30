/**
 * ui/static/js/views/models.js - Model catalog explorer, detail inspection modal,
 * archive submodels viewer, and ephemeral 3D mesh rendering interface.
 */

import {
  fetchModels,
  fetchModelFiles,
  fetchModelDetail,
  fetchCreators,
  fetchArchiveSubmodels,
  fetchRawArchiveTree,
  getEphemeralRenderUrl,
  renderModelView
} from '../api.js';
import { formatBytes, getFileExtension, renderStatusBadge, escapeHtml } from '../utils.js';

let currentModelPage = 1;
const modelPageSize = 24;
let totalModelCount = 0;
let creatorsLoaded = false;

// Active archive viewer state
let activeArchiveFileId = null;
let currentArchiveData = null;
let currentArchiveRawTree = null;
let currentArchiveViewMode = 'logical';

// Active ephemeral render lightbox state
let activeEphemeralSourceType = null;
let activeEphemeralId = null;
let activeEphemeralTitle = '';
let activeEphemeralBlobUrl = null;

const ARCHIVE_EXTENSIONS = ['.zip', '.rar', '.7z'];

function isArchiveFilename(filename) {
  if (!filename) return false;
  const lower = filename.toLowerCase();
  return ARCHIVE_EXTENSIONS.some(ext => lower.endsWith(ext));
}

function isStlFilename(filename) {
  if (!filename) return false;
  return filename.toLowerCase().endsWith('.stl');
}

/**
 * Injects responsive CSS styles for the 6-view dropdowns and render lightbox.
 */
function ensureRenderStyles() {
  if (document.getElementById('render-engine-styles')) return;
  const style = document.createElement('style');
  style.id = 'render-engine-styles';
  style.textContent = `
    .render-dropdown-wrap {
      position: relative;
      display: inline-block;
    }
    .btn-render-toggle {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 8px;
      font-size: 11px;
      background: #2a2a36;
      border: 1px solid #3d3d4e;
      color: #90caf9;
    }
    .btn-render-toggle:hover {
      background: #363646;
      border-color: #64b5f6;
    }
    .render-dropdown-menu {
      display: none;
      position: absolute;
      right: 0;
      top: calc(100% + 2px);
      min-width: 155px;
      background: #181822;
      border: 1px solid #333344;
      border-radius: 6px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.7);
      z-index: 1200;
      padding: 4px 0;
    }
    .render-dropdown-menu.show {
      display: block;
    }
    .render-dropdown-item {
      display: block;
      width: 100%;
      text-align: left;
      padding: 7px 12px;
      font-size: 11px;
      color: #d1d5db;
      background: transparent;
      border: none;
      cursor: pointer;
      transition: background 0.15s, color 0.15s;
    }
    .render-dropdown-item:hover {
      background: #28283a;
      color: #60a5fa;
    }
    .model-view-pills-row {
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .ephemeral-view-pills {
      display: flex;
      gap: 6px;
      justify-content: center;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }
    .ephemeral-view-pills .btn-pill.active {
      background: #2563eb;
      color: #ffffff;
      border-color: #3b82f6;
    }
    .ephemeral-image-stage {
      min-height: 440px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: #111116;
      border-radius: 8px;
      border: 1px solid #242432;
      overflow: hidden;
      padding: 12px;
    }
    .ephemeral-image-stage img {
      max-width: 100%;
      max-height: 65vh;
      object-fit: contain;
      border-radius: 4px;
    }
  `;
  document.head.appendChild(style);

  // Global dismiss listener for render dropdown menus
  document.addEventListener('click', () => {
    document.querySelectorAll('.render-dropdown-menu.show').forEach(el => el.classList.remove('show'));
  });
}

export async function populateCreatorsFilter() {
  const select = document.getElementById('model-creator-filter');
  if (!select) return;

  const currentSelection = select.value;
  try {
    const creators = await fetchCreators();
    let optionsHtml = '<option value="">All Creators</option>';
    optionsHtml += creators.map(c => {
      const name = c.designer_name || 'Unknown';
      const count = c.total_models || 0;
      return `<option value="${name.replace(/"/g, '&quot;')}">${name} (${count})</option>`;
    }).join('');

    select.innerHTML = optionsHtml;
    if (currentSelection) {
      select.value = currentSelection;
    }
    creatorsLoaded = true;
  } catch (err) {
    console.error('Failed to populate creators filter:', err);
  }
}

export async function loadModels(page = currentModelPage) {
  ensureRenderStyles();

  if (!creatorsLoaded) {
    await populateCreatorsFilter();
  }

  currentModelPage = Math.max(1, page);

  const searchInput = document.getElementById('model-search');
  const creatorSelect = document.getElementById('model-creator-filter');
  const tagInput = document.getElementById('model-tag-filter');
  const bonusCheck = document.getElementById('bonus-filter');

  const search = searchInput ? searchInput.value.trim() : '';
  const designer = creatorSelect ? creatorSelect.value : '';
  const tag = tagInput ? tagInput.value.trim() : '';
  const onlyBonus = bonusCheck ? bonusCheck.checked : false;

  const offset = (currentModelPage - 1) * modelPageSize;
  const container = document.getElementById('models-grid');

  if (container) {
    container.innerHTML = '<div class="grid-empty-msg">Loading models...</div>';
  }

  try {
    const data = await fetchModels({
      search,
      designer,
      tag,
      bonus: onlyBonus,
      limit: modelPageSize,
      offset
    });

    totalModelCount = data.total || 0;
    const models = data.models || [];

    if (container) {
      if (models.length === 0) {
        container.innerHTML = '<div class="grid-empty-msg">No matching models found in vault.</div>';
      } else {
        container.innerHTML = models.map(m => renderModelCard(m)).join('');
      }
    }

    updateModelsPagination(offset, models.length);
  } catch (err) {
    if (container) {
      container.innerHTML = `<div class="grid-empty-msg text-alert">Failed to load models: ${err.message}</div>`;
    }
  }
}

export function resetModelPageAndLoad() {
  loadModels(1);
}

export function changeModelPage(delta) {
  const totalPages = Math.ceil(totalModelCount / modelPageSize) || 1;
  const targetPage = currentModelPage + delta;
  if (targetPage >= 1 && targetPage <= totalPages) {
    loadModels(targetPage);
    const view = document.getElementById('view-models');
    if (view) view.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

export function filterByTag(tagName, event) {
  if (event) event.stopPropagation();
  const tagInput = document.getElementById('model-tag-filter');
  if (tagInput) {
    tagInput.value = tagName;
    resetModelPageAndLoad();
  }
}

function updateModelsPagination(offset, currentCount) {
  const pageInfo = document.getElementById('models-page-info');
  const pageNum = document.getElementById('models-page-num');
  const prevBtn = document.getElementById('models-prev-btn');
  const nextBtn = document.getElementById('models-next-btn');

  const totalPages = Math.ceil(totalModelCount / modelPageSize) || 1;
  const start = totalModelCount === 0 ? 0 : offset + 1;
  const end = offset + currentCount;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalModelCount.toLocaleString()} models`;
  if (pageNum) pageNum.textContent = `Page ${currentModelPage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentModelPage <= 1;
  if (nextBtn) nextBtn.disabled = currentModelPage >= totalPages;
}

export function renderModelCard(m) {
  let actionBtn = '';
  if (m.download_status === 'completed') {
    actionBtn = `<button type="button" class="btn-pill" disabled>Archived</button>`;
  } else if (m.download_status === 'queued') {
    actionBtn = `<button type="button" class="btn-pill btn-action-cancel" onclick="window.dequeueSingleModel(${m.id})">Cancel Queue</button>`;
  } else if (m.download_status === 'partial') {
    actionBtn = `<button type="button" class="btn-pill btn-action-queue" onclick="window.queueSingleModel(${m.id})">Download Remaining</button>`;
  } else {
    actionBtn = `<button type="button" class="btn-pill btn-action-queue" onclick="window.queueSingleModel(${m.id})">Queue Files</button>`;
  }

  const tags = m.tags || [];
  const tagsHtml = tags.length > 0
    ? `<div class="tag-list" onclick="event.stopPropagation()">
        ${tags.slice(0, 5).map(t => `<span class="tag-pill" onclick="window.filterByTag('${t.replace(/'/g, "\\'")}', event)">#${escapeHtml(t)}</span>`).join('')}
        ${tags.length > 5 ? `<span class="tag-pill tag-more" title="${escapeHtml(tags.slice(5).join(', '))}">+${tags.length - 5}</span>` : ''}
      </div>`
    : '';

  const supportBadgeHtml = m.pre_supported
    ? '<span class="status-badge status-completed font-xs">Pre-Supported</span>'
    : '<span class="status-badge status-not_downloaded font-xs">Unsupported</span>';

  const targetUrl = m.url || m.mmf_url;
  const mmfButtonHtml = targetUrl
    ? `<div class="card-bottom-action" onclick="event.stopPropagation()">
        <a href="${escapeHtml(targetUrl)}" 
           target="_blank" 
           rel="noopener noreferrer" 
           class="btn-mmf-fullwidth" 
           title="View original entry on MyMiniFactory">
          <span>View on MyMiniFactory</span>
          <span class="font-xs">&nearr;</span>
        </a>
      </div>`
    : '';

  const localCoverSrc = `/previews/${m.id}/cover.jpg`;
  const remoteCoverFallback = m.primary_image_url || '';

  return `
    <div class="card" onclick="window.openModelDetailModal(${m.id})" title="Click to view all metadata">
      <div class="thumb-wrap">
        ${remoteCoverFallback
          ? `<img src="${localCoverSrc}" onerror="this.onerror=null; this.src='${escapeHtml(remoteCoverFallback)}';" loading="lazy" alt="${escapeHtml(m.name)}">`
          : `<span class="text-dim">No Preview</span>`
        }
      </div>
      <div class="card-body">
        <div class="card-header-row">
          <span class="creator-name" title="${escapeHtml(m.designer_name || 'Unknown')}">${escapeHtml(m.designer_name || 'Unknown')}</span>
          <div class="card-badge-row">
            ${renderStatusBadge(m.download_status)}
            ${m.is_bonus ? `<span class="badge badge-bonus">Bonus</span>` : ''}
          </div>
        </div>
        <div class="card-title" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</div>
        ${tagsHtml}
        <div class="card-support-row" onclick="event.stopPropagation()">
          ${supportBadgeHtml}
        </div>
        <div class="model-card-actions" onclick="event.stopPropagation()">
          <button type="button" class="btn-pill" onclick="window.openFilesModal(${m.id})">Files (${m.file_count})</button>
          ${actionBtn}
          <button type="button" class="btn-pill" onclick="window.openAddToStashModal({ item_type: 'model', model_id: ${m.id}, title: '${escapeHtml(m.name).replace(/'/g, "\\'")}', creator: '${escapeHtml(m.designer_name || 'Unknown').replace(/'/g, "\\'")}' })" title="Add Model to Stash">+ Stash</button>
        </div>
        ${mmfButtonHtml}
      </div>
    </div>
  `;
}

/**
 * Builds the 6-view "Render ▾" dropdown HTML for any STL component or archive member.
 */
function renderSTLViewDropdown(sourceType, sourceId, title) {
  const safeTitle = escapeHtml(title || 'Component').replace(/'/g, "\\'");
  return `
    <div class="render-dropdown-wrap" onclick="event.stopPropagation()">
      <button type="button" class="btn-pill font-xs btn-render-toggle" onclick="window.toggleRenderDropdown(event, this)">
        <span>Render</span> <span class="font-xs">&blacktriangledown;</span>
      </button>
      <div class="render-dropdown-menu">
        <button type="button" class="render-dropdown-item" onclick="window.openEphemeralRenderModal('${sourceType}', ${sourceId}, 'front_left', '${safeTitle}')">Front-Left (3/4)</button>
        <button type="button" class="render-dropdown-item" onclick="window.openEphemeralRenderModal('${sourceType}', ${sourceId}, 'front_right', '${safeTitle}')">Front-Right</button>
        <button type="button" class="render-dropdown-item" onclick="window.openEphemeralRenderModal('${sourceType}', ${sourceId}, 'back_right', '${safeTitle}')">Back-Right</button>
        <button type="button" class="render-dropdown-item" onclick="window.openEphemeralRenderModal('${sourceType}', ${sourceId}, 'back_left', '${safeTitle}')">Back-Left</button>
        <button type="button" class="render-dropdown-item" onclick="window.openEphemeralRenderModal('${sourceType}', ${sourceId}, 'top_down', '${safeTitle}')">Top-Down</button>
        <button type="button" class="render-dropdown-item" onclick="window.openEphemeralRenderModal('${sourceType}', ${sourceId}, 'bottom_up', '${safeTitle}')">Bottom-Up</button>
      </div>
    </div>
  `;
}

export async function openModelDetailModal(modelId) {
  ensureRenderStyles();

  const modal = document.getElementById('model-detail-modal');
  const titleEl = document.getElementById('model-detail-title');
  const subtitleEl = document.getElementById('model-detail-subtitle');
  const bodyEl = document.getElementById('model-detail-body');

  if (modal) modal.style.display = 'flex';
  if (bodyEl) bodyEl.innerHTML = `<div class="loading-placeholder-lg">Fetching metadata for Model #${modelId}...</div>`;

  try {
    const data = await fetchModelDetail(modelId);

    if (titleEl) {
      titleEl.innerHTML = `${escapeHtml(data.name)} <span class="font-normal text-dim font-sm">(#${data.id})</span>`;
    }
    if (subtitleEl) {
      subtitleEl.innerHTML = `${escapeHtml(data.designer_name || 'Unknown')} &bull; ${data.total_files} file${data.total_files === 1 ? '' : 's'} (${formatBytes(data.total_size_bytes)})`;
    }

    const priceLabel = data.price_value !== null && data.price_value !== undefined
      ? `${data.price_currency || '$'}${Number(data.price_value).toFixed(2)}`
      : 'Free / Included in Tier';

    const categoryLabel = [data.parent_category, data.primary_category].filter(Boolean).join(' &rarr; ') || 'Uncategorized';
    const tags = data.tags || [];
    const files = data.files || [];
    const rawJsonStr = data.raw_metadata ? JSON.stringify(data.raw_metadata, null, 2) : null;

    const isCompleted = data.download_status === 'completed' ||
      (data.download_job && data.download_job.status === 'completed') ||
      (data.total_files > 0 && files.length > 0 && files.every(f => f.is_downloaded));

    const isQueued = data.download_status === 'queued' ||
      (data.download_job && ['pending', 'downloading'].includes(data.download_job.status));

    const isPartial = data.download_status === 'partial';

    let actionBtn = '';
    if (isCompleted) {
      actionBtn = `<button type="button" class="btn-pill btn-full" disabled>Archived to Vault</button>`;
    } else if (isQueued) {
      actionBtn = `<button type="button" class="btn-pill btn-action-cancel btn-full" onclick="window.dequeueSingleModel(${data.id}); window.closeModal('model-detail-modal');">Cancel Queue</button>`;
    } else if (isPartial) {
      actionBtn = `<button type="button" class="btn-pill btn-action-queue btn-full" onclick="window.queueSingleModel(${data.id}); window.closeModal('model-detail-modal');">Download Remaining</button>`;
    } else {
      actionBtn = `<button type="button" class="btn-pill btn-action-queue btn-full" onclick="window.queueSingleModel(${data.id}); window.closeModal('model-detail-modal');">Queue Files for Download</button>`;
    }

    const localCoverSrc = `/previews/${data.id}/cover.jpg`;
    const remoteCoverFallback = data.primary_image_url || '';
    const isExternal = data.source === 'external' || data.id >= 2_000_000_000;

    bodyEl.innerHTML = `
      <div class="detail-split-layout">
        <div class="detail-col-left">
          <div class="detail-media-card">
            ${remoteCoverFallback
              ? `<img id="detail-primary-preview-img" src="${localCoverSrc}" onerror="this.onerror=null; this.src='${escapeHtml(remoteCoverFallback)}';" class="detail-full-thumb" alt="${escapeHtml(data.name)}">`
              : '<div class="text-dim">No Preview Available</div>'
            }
            ${isExternal ? `
              <div class="model-view-pills-row">
                <span class="text-dim font-xs mr-1">Views:</span>
                <button type="button" class="btn-pill font-xs active" onclick="window.switchModelCoverView(${data.id}, 'front_left', this)">Front-L</button>
                <button type="button" class="btn-pill font-xs" onclick="window.switchModelCoverView(${data.id}, 'front_right', this)">Front-R</button>
                <button type="button" class="btn-pill font-xs" onclick="window.switchModelCoverView(${data.id}, 'back_right', this)">Back-R</button>
                <button type="button" class="btn-pill font-xs" onclick="window.switchModelCoverView(${data.id}, 'back_left', this)">Back-L</button>
                <button type="button" class="btn-pill font-xs" onclick="window.switchModelCoverView(${data.id}, 'top_down', this)">Top</button>
                <button type="button" class="btn-pill font-xs" onclick="window.switchModelCoverView(${data.id}, 'bottom_up', this)">Bottom</button>
              </div>
            ` : ''}
          </div>

          <div class="detail-actions-card">
            ${actionBtn}
            <button type="button" class="btn-pill btn-full" onclick="window.openAddToStashModal({ item_type: 'model', model_id: ${data.id}, title: '${escapeHtml(data.name).replace(/'/g, "\\'")}', creator: '${escapeHtml(data.designer_name || 'Unknown').replace(/'/g, "\\'")}' })">
              + Add Model to Stash
            </button>
            ${data.url ? `
              <a href="${data.url}" target="_blank" rel="noopener noreferrer" class="btn-pill btn-full">
                View on MyMiniFactory &rarr;
              </a>
            ` : ''}
          </div>

          <div class="detail-box">
            <div class="detail-box-heading">Indexed Tags (${tags.length})</div>
            <div class="tag-list">
              ${tags.length > 0
                ? tags.map(t => `<span class="tag-pill" onclick="window.closeModal('model-detail-modal'); window.filterByTag('${t.replace(/'/g, "\\'")}', event)">#${escapeHtml(t)}</span>`).join('')
                : '<span class="text-dim font-xs">No tags assigned.</span>'
              }
            </div>
          </div>
        </div>

        <div class="detail-col-right">
          <table class="detail-meta-table">
            <tbody>
              <tr>
                <td class="meta-label">Vault Status</td>
                <td>${renderStatusBadge(data.download_status || 'not_downloaded')}</td>
              </tr>
              <tr>
                <td class="meta-label">Model Name</td>
                <td><strong>${escapeHtml(data.name)}</strong></td>
              </tr>
              <tr>
                <td class="meta-label">Designer / Studio</td>
                <td>
                  <span class="text-accent font-semibold">${escapeHtml(data.designer_name || 'Unknown')}</span>
                  ${data.designer_username ? `<span class="text-dim font-xs"> (@${escapeHtml(data.designer_username)})</span>` : ''}
                </td>
              </tr>
              <tr>
                <td class="meta-label">Category</td>
                <td>${categoryLabel}</td>
              </tr>
              <tr>
                <td class="meta-label">Pre-Supported</td>
                <td>
                  <span class="badge ${data.pre_supported ? 'badge-month' : 'badge-ext'}">
                    ${data.pre_supported ? 'Yes (Pre-Supported)' : 'No (Unsupported)'}
                  </span>
                  ${data.is_bonus ? '<span class="badge badge-bonus ml-1">Bonus Drop</span>' : ''}
                  ${data.featured ? '<span class="badge badge-bundle ml-1">Featured</span>' : ''}
                </td>
              </tr>
              <tr>
                <td class="meta-label">Release Drop</td>
                <td class="font-mono font-sm">${data.release_month || 'Not assigned to drop'}</td>
              </tr>
              <tr>
                <td class="meta-label">Published At</td>
                <td class="font-mono font-sm">${data.published_at || 'Unknown'}</td>
              </tr>
              <tr>
                <td class="meta-label">Store Pricing</td>
                <td class="font-mono font-sm">${priceLabel}</td>
              </tr>
              <tr>
                <td class="meta-label">Community Stats</td>
                <td class="font-mono font-sm">${(data.views || 0).toLocaleString()} views &bull; ${(data.likes || 0).toLocaleString()} likes</td>
              </tr>
              <tr>
                <td class="meta-label">Asset Volume</td>
                <td class="font-mono font-sm"><strong>${data.total_files} files</strong> (${formatBytes(data.total_size_bytes)})</td>
              </tr>
              <tr>
                <td class="meta-label">Storage Identity</td>
                <td class="font-mono text-dim font-xs">
                  ${data.vault_path || data.raw_backup_path || 'metadata/model_' + data.id + '.json'}
                </td>
              </tr>
            </tbody>
          </table>

          ${data.description ? `
            <div class="detail-box mb-3">
              <div class="detail-box-heading">Description</div>
              <div class="detail-description-content">${escapeHtml(data.description)}</div>
            </div>
          ` : ''}

          <div class="detail-box">
            <div class="detail-box-heading">File Members (${files.length})</div>
            <table>
              <thead>
                <tr>
                  <th class="col-file-type">Type</th>
                  <th>Filename</th>
                  <th class="col-file-size">Size</th>
                  <th class="col-file-tier">Storage Tier</th>
                  <th class="col-file-link">Direct Link</th>
                  <th class="col-archive-action">Archive Inspection</th>
                  <th>Render</th>
                  <th>Stash</th>
                </tr>
              </thead>
              <tbody>
                ${files.length > 0 ? files.map(f => {
                  const tierLabel = f.storage_tier === 'nas'
                    ? '<span class="status-badge status-completed">NAS Vault</span>'
                    : (f.storage_tier === 'local'
                      ? '<span class="status-badge status-partial">Staging</span>'
                      : '<span class="status-badge status-not_downloaded">Pending</span>');

                  const isArchive = isArchiveFilename(f.filename);
                  const isStl = isStlFilename(f.filename);
                  const canInspect = isArchive && (f.storage_tier === 'nas' || f.is_downloaded);
                  const canRender = isStl && (f.storage_tier === 'nas' || f.is_downloaded);

                  return `
                    <tr>
                      <td><span class="badge-ext">${getFileExtension(f.filename)}</span></td>
                      <td class="cell-filename">${escapeHtml(f.filename || 'unnamed_file')}</td>
                      <td class="font-mono text-dim font-sm">${formatBytes(f.size_bytes)}</td>
                      <td>${tierLabel}</td>
                      <td>
                        ${f.download_url
                          ? `<a href="${f.download_url}" target="_blank" rel="noopener noreferrer" class="btn-link">Download &darr;</a>`
                          : '<span class="text-dim font-xs">No Link</span>'
                        }
                      </td>
                      <td>
                        ${canInspect
                          ? `<button type="button" class="btn-pill font-xs btn-inspect-archive" onclick="window.openArchiveViewerModal(${f.file_id})">Inspect Contents</button>`
                          : (isArchive ? '<span class="text-dim font-xs">Download to Peak</span>' : '<span class="text-dim font-xs">-</span>')
                        }
                      </td>
                      <td>
                        ${canRender
                          ? renderSTLViewDropdown('file', f.file_id, f.filename)
                          : '<span class="text-dim font-xs">-</span>'
                        }
                      </td>
                      <td>
                        <button type="button" class="btn-pill font-xs" onclick="window.openAddToStashModal({ item_type: 'file', model_id: ${data.id}, file_id: ${f.file_id}, title: '${escapeHtml(f.filename || 'unnamed_file').replace(/'/g, "\\'")}' })">+ Stash</button>
                      </td>
                    </tr>
                  `;
                }).join('') : '<tr><td colspan="8" class="td-empty">No separate files registered for this model.</td></tr>'}
              </tbody>
            </table>
          </div>

          ${rawJsonStr ? `
            <details class="detail-raw-details">
              <summary class="detail-raw-summary">[View Full Raw API Payload JSON]</summary>
              <pre class="detail-raw-pre">${escapeHtml(rawJsonStr)}</pre>
            </details>
          ` : ''}
        </div>
      </div>
    `;
  } catch (err) {
    if (bodyEl) {
      bodyEl.innerHTML = `<div class="loading-placeholder-lg text-alert">Failed to load model metadata: ${escapeHtml(err.message)}</div>`;
    }
  }
}

export async function openFilesModal(modelId) {
  ensureRenderStyles();

  const modal = document.getElementById('files-modal');
  const title = document.getElementById('files-modal-title');
  const subtitle = document.getElementById('files-modal-subtitle');
  const body = document.getElementById('files-modal-body');

  if (modal) modal.style.display = 'flex';
  if (body) body.innerHTML = `<div class="loading-placeholder">Fetching files for #${modelId}...</div>`;

  try {
    const data = await fetchModelFiles(modelId);
    if (title) title.innerHTML = `${escapeHtml(data.name)} <span class="text-dim font-sm font-normal">(#${data.id})</span>`;
    if (subtitle) subtitle.innerHTML = `${escapeHtml(data.designer_name)} &bull; <strong>${data.total_files} file${data.total_files === 1 ? '' : 's'}</strong> (${formatBytes(data.total_size_bytes)})`;

    if (!data.files || data.files.length === 0) {
      if (body) body.innerHTML = `<div class="loading-placeholder">No separate file entries found for this model.</div>`;
      return;
    }

    if (body) {
      body.innerHTML = `
        <table>
          <thead>
            <tr>
              <th class="col-file-type">Type</th>
              <th>Filename</th>
              <th class="col-file-size">Size</th>
              <th class="col-file-tier">Status</th>
              <th class="col-file-link">Direct Link</th>
              <th class="col-archive-action">Archive Inspection</th>
              <th>Render</th>
              <th>Stash</th>
            </tr>
          </thead>
          <tbody>
            ${data.files.map(f => {
              const isArchive = isArchiveFilename(f.filename);
              const isStl = isStlFilename(f.filename);
              const canInspect = isArchive && f.is_downloaded;
              const canRender = isStl && f.is_downloaded;

              return `
                <tr>
                  <td><span class="badge-ext">${getFileExtension(f.filename)}</span></td>
                  <td class="cell-filename">${escapeHtml(f.filename || 'unnamed_file')}</td>
                  <td class="font-mono text-dim font-sm">${formatBytes(f.size_bytes)}</td>
                  <td>${f.is_downloaded ? '<span class="text-accent font-bold">Downloaded</span>' : '<span class="text-dim">Pending</span>'}</td>
                  <td>
                    ${f.download_url
                      ? `<a href="${f.download_url}" target="_blank" rel="noopener noreferrer" class="btn-link">Download &darr;</a>`
                      : `<span class="text-dim font-xs">No Link</span>`
                    }
                  </td>
                  <td>
                    ${canInspect
                      ? `<button type="button" class="btn-pill font-xs btn-inspect-archive" onclick="window.openArchiveViewerModal(${f.file_id})">Inspect Contents</button>`
                      : (isArchive ? '<span class="text-dim font-xs">Download to Peak</span>' : '<span class="text-dim font-xs">-</span>')
                    }
                  </td>
                  <td>
                    ${canRender
                      ? renderSTLViewDropdown('file', f.file_id, f.filename)
                      : '<span class="text-dim font-xs">-</span>'
                    }
                  </td>
                  <td>
                    <button type="button" class="btn-pill font-xs" onclick="window.openAddToStashModal({ item_type: 'file', model_id: ${data.id}, file_id: ${f.file_id}, title: '${escapeHtml(f.filename || 'unnamed_file').replace(/'/g, "\\'")}' })">+ Stash</button>
                  </td>
                </tr>
              `;
            }).join('')}
          </tbody>
        </table>
      `;
    }
  } catch (err) {
    if (body) body.innerHTML = `<div class="loading-placeholder text-alert">Failed to load files: ${escapeHtml(err.message)}</div>`;
  }
}

// -----------------------------------------------------------------------------
// Archive Viewer & Peeker Modal Controller
// -----------------------------------------------------------------------------

export async function openArchiveViewerModal(fileId) {
  ensureRenderStyles();

  activeArchiveFileId = fileId;
  currentArchiveViewMode = 'logical';

  const modal = document.getElementById('modal-archive-viewer');
  const titleEl = document.getElementById('archive-viewer-title');
  const subtitleEl = document.getElementById('archive-viewer-subtitle');
  const bodyEl = document.getElementById('archive-viewer-body');
  const btnLogical = document.getElementById('btn-archive-view-logical');
  const btnRaw = document.getElementById('btn-archive-view-raw');

  if (modal) modal.style.display = 'flex';
  if (btnLogical && btnRaw) {
    btnLogical.classList.add('active');
    btnRaw.classList.remove('active');
  }
  if (bodyEl) bodyEl.innerHTML = '<div class="loading-placeholder-lg">Dissecting archive contents & loading submodels...</div>';

  try {
    const [submodelsData, rawTreeData] = await Promise.all([
      fetchArchiveSubmodels(fileId),
      fetchRawArchiveTree(fileId)
    ]);

    currentArchiveData = submodelsData;
    currentArchiveRawTree = rawTreeData;

    if (titleEl) {
      titleEl.innerHTML = `Archive Contents: <span class="text-main">${escapeHtml(submodelsData.filename)}</span>`;
    }
    if (subtitleEl) {
      subtitleEl.innerHTML = `
        Archive File #${submodelsData.file_id} &bull; Total Volume: <strong>${formatBytes(submodelsData.size_bytes)}</strong>
        &bull; <strong>${submodelsData.total_submodels} Logical Submodel${submodelsData.total_submodels === 1 ? '' : 's'}</strong>
        &bull; <strong>${rawTreeData.total_entries || 0} Total Archive Members</strong>
      `;
    }

    renderActiveArchiveView();
  } catch (err) {
    if (bodyEl) {
      bodyEl.innerHTML = `<div class="loading-placeholder-lg text-alert">Failed inspecting archive: ${escapeHtml(err.message)}</div>`;
    }
  }
}

export function switchArchiveViewMode(mode) {
  currentArchiveViewMode = mode;
  const btnLogical = document.getElementById('btn-archive-view-logical');
  const btnRaw = document.getElementById('btn-archive-view-raw');
  if (btnLogical && btnRaw) {
    btnLogical.classList.toggle('active', mode === 'logical');
    btnRaw.classList.toggle('active', mode === 'raw');
  }
  renderActiveArchiveView();
}

function renderActiveArchiveView() {
  const bodyEl = document.getElementById('archive-viewer-body');
  if (!bodyEl || !currentArchiveData) return;

  if (currentArchiveViewMode === 'logical') {
    renderLogicalSubmodelsView(bodyEl, currentArchiveData);
  } else {
    renderRawArchiveTreeView(bodyEl, currentArchiveRawTree);
  }
}

function renderLogicalSubmodelsView(container, data) {
  const submodels = data.submodels || [];

  if (submodels.length === 0) {
    container.innerHTML = `
      <div class="results-placeholder text-dim">
        No logical submodels found. The archive may not have been peaked yet or contains no printable 3D models.
      </div>
    `;
    return;
  }

  const cardsHtml = submodels.map(sm => {
    let badgesHtml = '';

    const scales = sm.scales || [];
    if (scales.length > 1) {
      badgesHtml += `<span class="badge badge-bundle font-xs" title="Multiple scales packaged together">Sizes: ${scales.join(' / ')}</span>`;
    }

    if (sm.has_presupported) {
      badgesHtml += '<span class="status-badge status-completed font-xs">Pre-Supported</span>';
    }
    if (sm.has_unsupported) {
      badgesHtml += '<span class="status-badge status-not_downloaded font-xs">Unsupported</span>';
    }
    if (sm.has_slicer_project) {
      badgesHtml += '<span class="status-badge status-queued font-xs">Slicer Project</span>';
    }

    (sm.formats || []).forEach(fmt => {
      badgesHtml += `<span class="badge badge-ext font-xs">${escapeHtml(fmt.toUpperCase())}</span>`;
    });

    const members = sm.members || [];
    const membersListHtml = members.map(m => {
      const isStl = (m.file_extension || '').toLowerCase() === '.stl' || (m.filename || '').toLowerCase().endsWith('.stl');
      return `
        <div class="submodel-member-row flex items-center justify-between gap-2 py-1">
          <div class="flex items-center gap-2 overflow-hidden">
            <span class="badge badge-ext font-xs">${escapeHtml(m.file_extension ? m.file_extension.replace('.', '').toUpperCase() : 'FILE')}</span>
            <span class="submodel-member-name font-mono truncate" title="${escapeHtml(m.internal_path)}">${escapeHtml(m.filename)}</span>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <span class="submodel-member-size font-mono text-dim font-xs">${formatBytes(m.size_bytes)}</span>
            ${isStl ? renderSTLViewDropdown('archive_member', m.id, m.filename) : ''}
          </div>
        </div>
      `;
    }).join('');

    const previewSrc = sm.preview_image_path
      ? (sm.preview_image_path.startsWith('http') ? sm.preview_image_path : `/${sm.preview_image_path}`)
      : '';

    // Identify candidate STL member to render if preview is missing
    const candidateStlMember = (members || []).find(m => {
      const ext = (m.file_extension || '').toLowerCase();
      const fn = (m.filename || '').toLowerCase();
      return (ext === '.stl' || fn.endsWith('.stl')) && !m.is_supported && !fn.includes('support');
    }) || (members || []).find(m => {
      const ext = (m.file_extension || '').toLowerCase();
      const fn = (m.filename || '').toLowerCase();
      return ext === '.stl' || fn.endsWith('.stl');
    });

    const imgElement = previewSrc
      ? `<a href="${previewSrc}" target="_blank" rel="noopener noreferrer" class="submodel-thumb-link" title="Click to view full preview">
           <img src="${previewSrc}" class="submodel-thumb" loading="lazy" alt="${escapeHtml(sm.name)}">
         </a>`
      : `<div class="submodel-no-thumb font-xs text-dim flex flex-col items-center justify-center p-3" id="submodel-thumb-container-${sm.id}">
           <span>No Preview Render</span>
           ${candidateStlMember ? `
             <button type="button" class="btn-pill font-xs mt-2 btn-action-primary" onclick="window.triggerSubmodelRender(${sm.id}, ${candidateStlMember.id}, this)">
               Render Preview
             </button>
           ` : ''}
         </div>`;

    return `
      <div class="card submodel-card">
        <div class="submodel-thumb-wrapper">
          ${imgElement}
        </div>
        <div class="submodel-content">
          <div class="submodel-header">
            <div class="submodel-title" title="${escapeHtml(sm.name)}">${escapeHtml(sm.name)}</div>
            <div class="submodel-badges">${badgesHtml}</div>
          </div>
          <div class="submodel-meta font-mono font-xs text-dim">
            <span>Volume: <strong>${formatBytes(sm.total_bytes)}</strong></span> &bull;
            <span>Members: <strong>${sm.total_files || members.length} files</strong></span>
          </div>
          <div class="submodel-path font-mono font-xs text-dim" title="${escapeHtml(sm.relative_dir || '/')}">
            Folder: ${escapeHtml(sm.relative_dir || '[Root Directory]')}
          </div>
          <details class="submodel-members-details">
            <summary class="submodel-members-summary">Inspect ${members.length} File Component${members.length === 1 ? '' : 's'}</summary>
            <div class="submodel-members-list">
              ${membersListHtml}
            </div>
          </details>

          <div class="footer mt-auto" style="padding-top:8px;">
            <button type="button" class="btn-pill font-xs btn-action-primary btn-full" onclick="window.openAddToStashModal({
              item_type: 'submodel',
              model_id: ${data.model_id},
              file_id: ${data.file_id},
              submodel_id: ${sm.id || (sm.members && sm.members[0] ? sm.members[0].submodel_id : 0)},
              title: '${escapeHtml(sm.name).replace(/'/g, "\\'")}'
            })">
              + Add Submodel to Stash
            </button>
          </div>
        </div>
      </div>
    `;
  }).join('');

  container.innerHTML = `
    <div class="submodel-grid">
      ${cardsHtml}
    </div>
  `;
}

function renderRawArchiveTreeView(container, rawData) {
  const entries = (rawData && rawData.entries) || [];
  const modelId = currentArchiveData?.model_id || '';
  const fileId = currentArchiveData?.file_id || '';

  if (entries.length === 0) {
    container.innerHTML = `
      <div class="results-placeholder text-dim">
        No archive table of contents entries recorded.
      </div>
    `;
    return;
  }

  const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.webp'];

  const rowsHtml = entries.map((entry, idx) => {
    const ext = (entry.file_extension || '').toLowerCase();
    const isImage = IMAGE_EXTS.includes(ext);
    const isStl = ext === '.stl' || (entry.filename || '').toLowerCase().endsWith('.stl');

    const cleanDiskName = (entry.filename || '').replace(/[^\w\-_\.]/g, '_');
    const imagePreviewUrl = `/previews/${modelId}/${fileId}/${cleanDiskName}`;

    let pathDisplay = `<span class="font-mono">${escapeHtml(entry.internal_path)}</span>`;
    if (isImage) {
      pathDisplay = `
        <a href="${imagePreviewUrl}" target="_blank" rel="noopener noreferrer" class="cell-clickable-title font-mono" title="Click to view extracted preview">
          ${escapeHtml(entry.internal_path)} <span class="text-accent font-xs">&boxbox;</span>
        </a>
      `;
    }

    return `
      <tr>
        <td class="font-mono text-dim font-xs">${idx + 1}</td>
        <td class="cell-filename">${pathDisplay}</td>
        <td class="font-mono text-dim font-sm">${formatBytes(entry.size_bytes)}</td>
        <td>
          <span class="badge ${isImage ? 'badge-bonus' : 'badge-ext'} font-xs">
            ${escapeHtml(ext ? ext.replace('.', '').toUpperCase() : 'FILE')}
          </span>
        </td>
        <td>
          ${entry.is_supported
            ? '<span class="status-badge status-completed font-xs">Supported</span>'
            : (['.stl', '.lys', '.ctb', '.ags'].includes(ext)
              ? '<span class="status-badge status-not_downloaded font-xs">Raw</span>'
              : '<span class="text-dim font-xs">-</span>')
          }
        </td>
        <td class="cell-creator-accent font-xs">
          ${escapeHtml(entry.submodel_name || '[Asset / Document]')}
        </td>
        <td>
          ${isStl
            ? renderSTLViewDropdown('archive_member', entry.id, entry.filename)
            : '<span class="text-dim font-xs">-</span>'
          }
        </td>
        <td>
          <button type="button" class="btn-pill font-xs" onclick="window.openAddToStashModal({
            item_type: 'archive_member',
            model_id: ${modelId},
            file_id: ${fileId},
            submodel_id: ${entry.submodel_id || 'null'},
            archive_member_id: ${entry.id || entry.archive_member_id || entry.member_id || 0},
            title: '${escapeHtml(entry.filename).replace(/'/g, "\\'")}'
          })">+ Stash</button>
        </td>
      </tr>
    `;
  }).join('');

  container.innerHTML = `
    <div class="archive-tree-container">
      <table>
        <thead>
          <tr>
            <th style="width:40px;">#</th>
            <th>Archive Internal Path (Click images to open preview)</th>
            <th style="width:110px;">Size</th>
            <th style="width:70px;">Type</th>
            <th style="width:100px;">Support</th>
            <th style="width:180px;">Assigned Submodel</th>
            <th style="width:85px;">Render</th>
            <th style="width:75px;">Stash</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    </div>
  `;
}

// -----------------------------------------------------------------------------
// Interactive 3D Mesh & Component Ephemeral Render Handlers
// -----------------------------------------------------------------------------

/**
 * Toggles a 6-view "Render ▾" dropdown popup.
 */
export function toggleRenderDropdown(event, btn) {
  event.stopPropagation();
  const menu = btn.nextElementSibling;
  const isAlreadyOpen = menu && menu.classList.contains('show');

  document.querySelectorAll('.render-dropdown-menu.show').forEach(el => el.classList.remove('show'));

  if (menu && !isAlreadyOpen) {
    menu.classList.add('show');
  }
}

/**
 * Renders an inline preview on demand for an un-rendered submodel card.
 */
export async function triggerSubmodelRender(smId, memberId, btn) {
  const container = document.getElementById(`submodel-thumb-container-${smId}`);
  if (!container) return;

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Rendering...';
  }

  try {
    const res = await fetch('/api/render/stl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_type: 'archive_member',
        id: memberId,
        view: 'front_left'
      })
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }

    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);

    container.outerHTML = `
      <a href="${objectUrl}" target="_blank" rel="noopener noreferrer" class="submodel-thumb-link" title="Rendered ephemeral preview">
        <img src="${objectUrl}" class="submodel-thumb" alt="Rendered Submodel">
      </a>
    `;
  } catch (err) {
    console.error('Submodel render failed:', err);
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Render Failed (Retry)';
    }
  }
}

/**
 * Switches the perspective thumbnail of an external model in the Model Detail Modal.
 */
export async function switchModelCoverView(modelId, view, btn) {
  const imgEl = document.getElementById('detail-primary-preview-img');
  if (!imgEl) return;

  const parentRow = btn ? btn.parentElement : null;
  if (parentRow) {
    parentRow.querySelectorAll('.btn-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    btn.textContent = '...';
  }

  try {
    const res = await renderModelView(modelId, view);
    if (res && res.image_url) {
      imgEl.src = `${res.image_url}?_t=${Date.now()}`;
    }
  } catch (err) {
    alert(`Failed rendering view: ${err.message}`);
  } finally {
    if (parentRow && btn) {
      const labelMap = {
        front_left: 'Front-L',
        front_right: 'Front-R',
        back_right: 'Back-R',
        back_left: 'Back-L',
        top_down: 'Top',
        bottom_up: 'Bottom'
      };
      btn.textContent = labelMap[view] || view;
    }
  }
}

/**
 * Ensures the transient lightbox modal exists in document.body.
 */
function ensureEphemeralModalExists() {
  if (document.getElementById('modal-ephemeral-render')) return;

  const modalHtml = `
    <div class="modal-backdrop" id="modal-ephemeral-render" style="display:none; z-index:9999;">
      <div class="modal modal-lg" style="max-width:850px;">
        <div class="modal-header">
          <div>
            <h2 class="modal-title-accent" id="ephemeral-render-title">Component 3D Render</h2>
            <div class="modal-subtitle font-md" id="ephemeral-render-subtitle">Angle: Front-Left (3/4 Profile)</div>
          </div>
          <button type="button" class="tab-btn" onclick="window.closeEphemeralRenderModal()">Close</button>
        </div>
        <div class="modal-body">
          <div class="ephemeral-view-pills" id="ephemeral-pills-container">
            <button type="button" class="btn-pill font-xs pill-view active" data-view="front_left" onclick="window.switchEphemeralView('front_left')">Front-Left (3/4)</button>
            <button type="button" class="btn-pill font-xs pill-view" data-view="front_right" onclick="window.switchEphemeralView('front_right')">Front-Right</button>
            <button type="button" class="btn-pill font-xs pill-view" data-view="back_right" onclick="window.switchEphemeralView('back_right')">Back-Right</button>
            <button type="button" class="btn-pill font-xs pill-view" data-view="back_left" onclick="window.switchEphemeralView('back_left')">Back-Left</button>
            <button type="button" class="btn-pill font-xs pill-view" data-view="top_down" onclick="window.switchEphemeralView('top_down')">Top-Down</button>
            <button type="button" class="btn-pill font-xs pill-view" data-view="bottom_up" onclick="window.switchEphemeralView('bottom_up')">Bottom-Up</button>
          </div>
          <div class="ephemeral-image-stage" id="ephemeral-stage">
            <div class="loading-placeholder">Initializing Blender preview stream...</div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.body.insertAdjacentHTML('beforeend', modalHtml);
}

/**
 * Opens the transient 6-view preview lightbox for any loose STL or internal archive member.
 */
export async function openEphemeralRenderModal(sourceType, sourceId, initialView = 'front_left', title = '') {
  ensureRenderStyles();
  ensureEphemeralModalExists();

  activeEphemeralSourceType = sourceType;
  activeEphemeralId = sourceId;
  activeEphemeralTitle = title || 'Mesh Component';

  const modal = document.getElementById('modal-ephemeral-render');
  const titleEl = document.getElementById('ephemeral-render-title');

  if (titleEl) {
    titleEl.textContent = activeEphemeralTitle;
  }
  if (modal) modal.style.display = 'flex';

  await switchEphemeralView(initialView);
}

export function closeEphemeralRenderModal() {
  const modal = document.getElementById('modal-ephemeral-render');
  if (modal) modal.style.display = 'none';

  if (activeEphemeralBlobUrl) {
    URL.revokeObjectURL(activeEphemeralBlobUrl);
    activeEphemeralBlobUrl = null;
  }
}

/**
 * Switches camera angle within the ephemeral preview lightbox.
 */
export async function switchEphemeralView(viewName) {
  const stage = document.getElementById('ephemeral-stage');
  const subtitleEl = document.getElementById('ephemeral-render-subtitle');
  const container = document.getElementById('ephemeral-pills-container');

  const viewLabels = {
    front_left: 'Front-Left (3/4 Profile)',
    front_right: 'Front-Right Flank',
    back_right: 'Back-Right Flank',
    back_left: 'Back-Left Profile',
    top_down: 'Top-Down Zenith',
    bottom_up: 'Bottom-Up Nadir'
  };

  if (subtitleEl) {
    subtitleEl.textContent = `Angle: ${viewLabels[viewName] || viewName}`;
  }

  if (container) {
    container.querySelectorAll('.pill-view').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === viewName);
    });
  }

  if (stage) {
    stage.innerHTML = `
      <div class="flex flex-col items-center justify-center gap-3">
        <div class="loading-placeholder">Rendering ${viewLabels[viewName]} in Blender...</div>
        <span class="text-dim font-xs">Extracting geometry into transient memory buffer</span>
      </div>
    `;
  }

  try {
    const res = await fetch('/api/render/stl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_type: activeEphemeralSourceType,
        id: activeEphemeralId,
        view: viewName
      })
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }

    if (activeEphemeralBlobUrl) {
      URL.revokeObjectURL(activeEphemeralBlobUrl);
    }

    const blob = await res.blob();
    activeEphemeralBlobUrl = URL.createObjectURL(blob);

    if (stage) {
      stage.innerHTML = `
        <img src="${activeEphemeralBlobUrl}" alt="${escapeHtml(activeEphemeralTitle)} (${viewName})" />
      `;
    }
  } catch (err) {
    if (stage) {
      stage.innerHTML = `
        <div class="text-alert p-4 text-center">
          <strong>Blender Render Error:</strong><br>
          <span class="font-xs font-mono">${escapeHtml(err.message)}</span>
        </div>
      `;
    }
  }
}

// -----------------------------------------------------------------------------
// Window Bindings for Global DOM Event Handlers
// -----------------------------------------------------------------------------
Object.assign(window, {
  loadModels,
  resetModelPageAndLoad,
  changeModelPage,
  filterByTag,
  openModelDetailModal,
  openFilesModal,
  openArchiveViewerModal,
  switchArchiveViewMode,
  toggleRenderDropdown,
  triggerSubmodelRender,
  switchModelCoverView,
  openEphemeralRenderModal,
  closeEphemeralRenderModal,
  switchEphemeralView
});
