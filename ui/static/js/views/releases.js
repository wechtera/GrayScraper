import { fetchReleases, fetchAudit, fetchModels, fetchCreators, queueDownloads, dequeueDownloads } from '../api.js';
import { formatBytes, renderStatusBadge, escapeHtml } from '../utils.js';
import { renderModelCard } from './models.js';

let currentReleasePage = 1;
const releasePageSize = 36;
let totalReleaseCount = 0;
let releaseCreatorsLoaded = false;

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'
];

function formatMonthTitle(monthStr) {
  if (!monthStr || monthStr === 'Unassigned') return 'Unassigned Drops';
  const parts = monthStr.split('-');
  if (parts.length === 2) {
    const year = parseInt(parts[0], 10);
    const monthNum = parseInt(parts[1], 10);
    if (monthNum >= 1 && monthNum <= 12) {
      return `${MONTH_NAMES[monthNum - 1]} ${year}`;
    }
  }
  return monthStr;
}

export async function populateReleaseCreatorsFilter() {
  const select = document.getElementById('release-creator-filter');
  if (!select) return;

  const currentSelection = select.value;
  try {
    const creators = await fetchCreators();
    let optionsHtml = '<option value="">All Creators</option>';
    optionsHtml += creators
      .filter(c => c.designer_name && c.designer_name !== 'Unknown Designer' && c.designer_name !== 'Unknown')
      .map(c => {
        const name = c.designer_name;
        const count = c.total_models || 0;
        return `<option value="${escapeHtml(name)}">${escapeHtml(name)} (${count})</option>`;
      }).join('');

    select.innerHTML = optionsHtml;
    if (currentSelection) select.value = currentSelection;
    releaseCreatorsLoaded = true;
  } catch (err) {
    console.error('Failed to populate release creators filter:', err);
  }
}

export function filterReleaseByCreator(creatorName) {
  const select = document.getElementById('release-creator-filter');
  const searchInput = document.getElementById('release-search');
  if (select) select.value = creatorName;
  if (searchInput) searchInput.value = '';
  resetReleasePageAndLoad();
}

export async function loadAudit(designer = '') {
  const auditPanel = document.getElementById('audit-panel');
  if (!auditPanel) return;

  const data = await fetchAudit(designer);
  if (!data.has_data) {
    auditPanel.style.display = 'none';
    return;
  }

  auditPanel.style.display = 'block';
  document.getElementById('audit-title').innerHTML = designer
    ? `Catalog Continuity &amp; Statistical Audit &bull; <span class="text-accent">${escapeHtml(designer)}</span>`
    : `Entire Library Continuity &amp; Statistical Audit`;

  document.getElementById('audit-span').textContent = `${data.oldest_month} \u2192 ${data.latest_month}`;
  document.getElementById('audit-coverage').textContent = `${data.actual_months_count} / ${data.expected_months_count} Mo (${data.coverage_pct}%)`;
  document.getElementById('audit-avg').textContent = `${data.avg_models} \u00B1 ${data.std_dev}`;
  document.getElementById('audit-bonuses').textContent = `${data.total_bonus_models} items`;

  const anomaliesDiv = document.getElementById('audit-anomalies');
  
  // Compact summary for whole-library view to preserve vertical real estate
  if (!designer) {
    anomaliesDiv.innerHTML = `
      <div class="anomaly-group">
        <span class="anomaly-label text-dim">Outliers:</span>
        <span class="font-xs text-dim">${data.low_outliers.length} low volume, ${data.high_outliers.length} high volume drops. Select a creator to inspect specific gaps.</span>
      </div>
    `;
    return;
  }

  let html = '';
  if (data.missing_count > 0) {
    html += `
      <div class="anomaly-group">
        <span class="anomaly-label text-alert">Missing Months (${data.missing_count}):</span>
        ${data.missing_months.map(m => `<span class="gap-pill" title="Check MMF library or studio hiatus">${m}</span>`).join('')}
      </div>
    `;
  } else {
    html += `
      <div class="anomaly-group">
        <span class="anomaly-label text-accent">Chronology:</span>
        <span class="ok-pill">Continuous release history: 0 calendar months missing</span>
      </div>
    `;
  }

  if (data.low_outliers && data.low_outliers.length > 0) {
    html += `
      <div class="anomaly-group">
        <span class="anomaly-label text-warn">Low Volume:</span>
        ${data.low_outliers.map(o => `<span class="low-pill">${o.month} (${o.count} models)</span>`).join('')}
      </div>
    `;
  }

  if (data.high_outliers && data.high_outliers.length > 0) {
    html += `
      <div class="anomaly-group">
        <span class="anomaly-label text-info">High Volume:</span>
        ${data.high_outliers.map(o => `<span class="high-pill">${o.month} (+${o.count} models)</span>`).join('')}
      </div>
    `;
  }

  anomaliesDiv.innerHTML = html;
}

export async function loadReleases(page = currentReleasePage) {
  if (!releaseCreatorsLoaded) {
    await populateReleaseCreatorsFilter();
  }

  currentReleasePage = Math.max(1, page);

  const searchInput = document.getElementById('release-search');
  const creatorSelect = document.getElementById('release-creator-filter');
  const bundleCheck = document.getElementById('bundles-only');

  const searchVal = searchInput ? searchInput.value.trim() : '';
  const selectedCreator = creatorSelect ? creatorSelect.value : '';
  const designer = selectedCreator || searchVal;
  const bundlesOnly = bundleCheck ? bundleCheck.checked : false;

  loadAudit(designer);

  const offset = (currentReleasePage - 1) * releasePageSize;
  const container = document.getElementById('releases-grid');

  if (container) {
    container.innerHTML = '<div class="grid-empty-msg">Loading release drops...</div>';
  }

  try {
    const data = await fetchReleases({
      designer,
      bundlesOnly,
      limit: releasePageSize,
      offset
    });

    const releases = Array.isArray(data) ? data : (data.releases || []);
    totalReleaseCount = Array.isArray(data) ? releases.length : (data.total || 0);

    if (container) {
      if (releases.length === 0) {
        container.innerHTML = '<div class="grid-empty-msg">No matching monthly drops found.</div>';
        updateReleasesPagination(offset, 0);
        return;
      }

      // Group into ordered months
      const monthsMap = new Map();
      for (const r of releases) {
        const monthKey = r.release_month || 'Unassigned';
        if (!monthsMap.has(monthKey)) {
          monthsMap.set(monthKey, []);
        }
        monthsMap.get(monthKey).push(r);
      }

      let html = '';
      for (const [monthKey, monthReleases] of monthsMap.entries()) {
        // Exclude Unknown Designer from the monthly chips
        const creatorsInMonth = Array.from(new Set(
          monthReleases
            .map(r => r.designer_name)
            .filter(name => name && name !== 'Unknown Designer' && name !== 'Unknown')
        ));
        const totalModelsInMonth = monthReleases.reduce((acc, r) => acc + (r.model_count || 0), 0);
        const totalBytesInMonth = monthReleases.reduce((acc, r) => acc + (r.total_size_bytes || 0), 0);

        const creatorChipsHtml = creatorsInMonth.map(c => `
          <button type="button" class="release-creator-chip" onclick="window.filterReleaseByCreator('${escapeHtml(c).replace(/'/g, "\\'")}')" title="Filter by ${escapeHtml(c)}">
            ${escapeHtml(c)}
          </button>
        `).join('');

        const cardsHtml = monthReleases.map(r => {
          const isQueued = r.download_status === 'queued';
          const isCompleted = r.download_status === 'completed';

          let queueBtn = '';
          if (isCompleted) {
            queueBtn = `<button type="button" class="btn-pill" disabled>Archived</button>`;
          } else if (isQueued) {
            queueBtn = `<button type="button" class="btn-pill btn-action-cancel" onclick="window.cancelReleaseQueue('${r.id}')">Cancel Pack Queue</button>`;
          } else {
            queueBtn = `<button type="button" class="btn-pill btn-action-queue" onclick="window.queueReleasePack('${r.id}')">Queue Entire Pack</button>`;
          }

          const localReleaseSrc = r.sample_model_id ? `/previews/${r.sample_model_id}/cover.jpg` : (r.sample_image_url || '');
          const remoteFallback = r.sample_image_url || '';
          const escapedDesigner = escapeHtml(r.designer_name).replace(/'/g, "\\'");

          return `
            <div class="card cursor-pointer" onclick="window.openReleaseModal('${escapedDesigner}', '${r.release_month}')" title="Click to view models in this release">
              <div class="thumb-wrap">
                ${localReleaseSrc
                  ? `<img src="${localReleaseSrc}" onerror="this.onerror=null; this.src='${escapeHtml(remoteFallback)}';" loading="lazy" alt="${escapeHtml(r.designer_name)}">`
                  : `<span class="text-dim font-xs">No Preview</span>`
                }
              </div>
              <div class="card-body">
                <div class="card-header-row">
                  <span class="badge badge-month font-mono font-xs">${r.release_month}</span>
                  <div class="card-badge-row">
                    ${renderStatusBadge(r.download_status)}
                    ${r.is_likely_bundle ? `<span class="badge badge-bundle">Patreon / Tribe</span>` : ''}
                  </div>
                </div>
                <div class="card-title">${escapeHtml(r.designer_name)}</div>
                <div class="release-stats-row font-mono">
                  ${r.model_count} core models &bull; ${r.total_files} files (${formatBytes(r.total_size_bytes)})
                </div>
                <div class="footer" style="justify-content: flex-end;">
                  <div class="footer-actions" onclick="event.stopPropagation()">${queueBtn}</div>
                </div>
              </div>
            </div>
          `;
        }).join('');

        // CHANGED: Use <div> instead of <header> to avoid conflicting with main navbar styling
        html += `
          <section class="release-month-section">
            <div class="release-month-header">
              <div class="release-month-top">
                <div class="release-month-title-wrap">
                  <h3 class="release-month-title">${escapeHtml(formatMonthTitle(monthKey))}</h3>
                  <span class="badge badge-month font-mono font-xs">${monthKey}</span>
                </div>
                <div class="release-month-stats font-mono">
                  <span><strong>${monthReleases.length}</strong> drop${monthReleases.length === 1 ? '' : 's'}</span>
                  <span>&bull;</span>
                  <span><strong>${totalModelsInMonth}</strong> models</span>
                  <span>&bull;</span>
                  <span><strong>${formatBytes(totalBytesInMonth)}</strong></span>
                </div>
              </div>

              ${creatorsInMonth.length > 0 ? `
                <div class="release-month-creators-bar">
                  <span class="release-month-creators-label">Creators in this month (${creatorsInMonth.length}):</span>
                  ${creatorChipsHtml}
                </div>
              ` : ''}
            </div>

            <div class="grid releases-month-grid">
              ${cardsHtml}
            </div>
          </section>
        `;
      }

      container.innerHTML = html;
    }

    updateReleasesPagination(offset, releases.length);
  } catch (err) {
    if (container) {
      container.innerHTML = `<div class="grid-empty-msg text-alert">Failed to load releases: ${err.message}</div>`;
    }
  }
}

export function resetReleasePageAndLoad() {
  loadReleases(1);
}

export function changeReleasePage(delta) {
  const totalPages = Math.ceil(totalReleaseCount / releasePageSize) || 1;
  const targetPage = currentReleasePage + delta;
  if (targetPage >= 1 && targetPage <= totalPages) {
    loadReleases(targetPage);
    const view = document.getElementById('view-releases');
    if (view) view.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function updateReleasesPagination(offset, currentCount) {
  const pageInfo = document.getElementById('releases-page-info');
  const pageNum = document.getElementById('releases-page-num');
  const prevBtn = document.getElementById('releases-prev-btn');
  const nextBtn = document.getElementById('releases-next-btn');

  const totalPages = Math.ceil(totalReleaseCount / releasePageSize) || 1;
  const start = totalReleaseCount === 0 ? 0 : offset + 1;
  const end = offset + currentCount;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalReleaseCount.toLocaleString()} drops`;
  if (pageNum) pageNum.textContent = `Page ${currentReleasePage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentReleasePage <= 1;
  if (nextBtn) nextBtn.disabled = currentReleasePage >= totalPages;
}

export async function openReleaseModal(designer, month) {
  document.getElementById('modal-title').textContent = `${designer} \u2014 Release: ${month}`;
  document.getElementById('release-modal').style.display = 'flex';

  const data = await fetchModels({ designer, month, limit: 150 });
  const container = document.getElementById('modal-models-grid');
  container.innerHTML = data.models.map(m => renderModelCard(m)).join('');
}

export async function queueReleasePack(releaseId) {
  await queueDownloads({ release_id: releaseId });
  window.refreshCurrentView();
}

export async function cancelReleaseQueue(releaseId) {
  const parts = releaseId.split('_');
  const data = await fetchModels({ designer: parts[0], month: parts[1], limit: 200 });
  const ids = data.models.map(m => m.id);
  if (ids.length > 0) {
    await dequeueDownloads({ model_ids: ids });
  }
  window.refreshCurrentView();
}

window.filterReleaseByCreator = filterReleaseByCreator;
window.resetReleasePageAndLoad = resetReleasePageAndLoad;
