import { fetchCreators } from '../api.js';

export async function loadCreators() {
  const creators = await fetchCreators();

  const tbody = document.getElementById('creators-table-body');
  if (tbody) {
    tbody.innerHTML = creators.map(c => `
      <tr>
        <td class="cell-name">${c.designer_name}</td>
        <td class="font-mono">${c.total_models}</td>
        <td><span class="text-bonus font-mono">${c.bonus_models}</span></td>
        <td class="font-mono">${c.supported_models}</td>
        <td class="font-mono">${c.active_months}</td>
        <td class="cell-date-range font-mono">${c.first_seen || ''} &rarr; ${c.latest_seen || ''}</td>
        <td>
          <span class="btn-link" onclick="window.filterByCreator('${c.designer_name.replace(/'/g, "\\'")}')">
            Inspect Drops &rarr;
          </span>
        </td>
      </tr>
    `).join('');
  }

  const select1 = document.getElementById('model-creator-filter');
  const select2 = document.getElementById('pending-creator-filter');
  const opts = '<option value="">All Creators</option>' + creators.map(c => `<option value="${c.designer_name}">${c.designer_name} (${c.total_models})</option>`).join('');
  if (select1) select1.innerHTML = opts;
  if (select2) select2.innerHTML = opts;
}

export function filterByCreator(name) {
  window.switchTab('releases');
  const releaseSearch = document.getElementById('release-search');
  if (releaseSearch) releaseSearch.value = name;
  window.loadReleases();
}
