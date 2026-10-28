import {
  fetchModels,
  fetchModelFiles,
  fetchModelDetail,
  fetchCreators,
  fetchArchiveSubmodels,
  fetchRawArchiveTree
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

const ARCHIVE_EXTENSIONS = ['.zip', '.rar', '.7z'];

function isArchiveFilename(filename) {
  if (!filename) return false;
  const lower = filename.toLowerCase();
  return ARCHIVE_EXTENSIONS.some(ext => lower.endsWith(ext));
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

  // Local-first thumbnail with remote CDN fallback
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

export async function openModelDetailModal(modelId) {
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

    bodyEl.innerHTML = `
      <div class="detail-split-layout">
        <div class="detail-col-left">
          <div class="detail-media-card">
            ${remoteCoverFallback
              ? `<img src="${localCoverSrc}" onerror="this.onerror=null; this.src='${escapeHtml(remoteCoverFallback)}';" class="detail-full-thumb" alt="${escapeHtml(data.name)}">`
              : '<div class="text-dim">No Preview Available</div>'
            }
          </div>

          <div class="detail-actions-card">
            ${actionBtn}
            <button type="button" class="btn-pill btn-full" onclick="window.openAddToStashModal({ item_type: 'model', model_id: ${data.id}, title: '${escapeHtml(data.name).replace(/'/g, "\\'")}', creator: '${escapeHtml(data.designer_name || 'Unknown').replace(/'/g, "\\'")}' })">
              + Add Model to Stash
            </button>
            <a href="${data.url}" target="_blank" rel="noopener noreferrer" class="btn-pill btn-full">
              View on MyMiniFactory &rarr;
            </a>
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
                  const canInspect = isArchive && (f.storage_tier === 'nas' || f.is_downloaded);

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
                        <button type="button" class="btn-pill font-xs" onclick="window.openAddToStashModal({ item_type: 'file', model_id: ${data.id}, file_id: ${f.file_id}, title: '${escapeHtml(f.filename || 'unnamed_file').replace(/'/g, "\\'")}' })">+ Stash</button>
                      </td>
                    </tr>
                  `;
                }).join('') : '<tr><td colspan="7" class="td-empty">No separate files registered for this model.</td></tr>'}
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
              <th>Stash</th>
            </tr>
          </thead>
          <tbody>
            ${data.files.map(f => {
              const isArchive = isArchiveFilename(f.filename);
              const canInspect = isArchive && f.is_downloaded;

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
    const membersListHtml = members.map(m => `
      <div class="submodel-member-row">
        <span class="badge badge-ext font-xs">${escapeHtml(m.file_extension ? m.file_extension.replace('.', '').toUpperCase() : 'FILE')}</span>
        <span class="submodel-member-name font-mono" title="${escapeHtml(m.internal_path)}">${escapeHtml(m.filename)}</span>
        <span class="submodel-member-size font-mono text-dim font-xs">${formatBytes(m.size_bytes)}</span>
      </div>
    `).join('');

    const previewSrc = sm.preview_image_path
      ? (sm.preview_image_path.startsWith('http') ? sm.preview_image_path : `/${sm.preview_image_path}`)
      : '';

    const imgElement = previewSrc
      ? `<a href="${previewSrc}" target="_blank" rel="noopener noreferrer" class="submodel-thumb-link" title="Click to view full preview">
           <img src="${previewSrc}" class="submodel-thumb" loading="lazy" alt="${escapeHtml(sm.name)}">
         </a>`
      : `<div class="submodel-no-thumb font-xs text-dim">No Preview Render</div>`;

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
