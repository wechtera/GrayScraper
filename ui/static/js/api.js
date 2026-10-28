async function request(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let errorDetail = `HTTP ${res.status}`;
    try {
      const errJson = await res.json();
      if (errJson.error) errorDetail = errJson.error;
    } catch (_) {}
    throw new Error(errorDetail);
  }
  return res.json();
}

export async function fetchMetrics() {
  return request('/api/metrics');
}

export async function fetchStorageStatus() {
  return request('/api/storage/status');
}

export async function saveStorageConfig(payload) {
  const body = typeof payload === 'string' ? { path: payload } : payload;
  return request('/api/storage/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

export async function fetchDownloadsStatus() {
  return request('/api/downloads/status');
}

export async function fetchDownloadHistory({ status = '', sort = '', limit = 50, offset = 0 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (sort) params.set('sort', sort);
  params.set('limit', limit);
  params.set('offset', offset);
  return request(`/api/downloads/history?${params.toString()}`);
}

export async function queueDownloads(payload) {
  return request('/api/downloads/queue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function dequeueDownloads(payload) {
  return request('/api/downloads/queue', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function controlDownloads(action) {
  return request('/api/downloads/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  });
}

export async function fetchAudit(designer = '') {
  return request(`/api/audit?designer=${encodeURIComponent(designer)}`);
}

export async function fetchReleases({ designer = '', bundlesOnly = false, limit = 24, offset = 0 } = {}) {
  const params = new URLSearchParams();
  if (designer) params.set('designer', designer);
  if (bundlesOnly) params.set('bundles_only', '1');
  params.set('limit', limit);
  params.set('offset', offset);
  return request(`/api/releases?${params.toString()}`);
}

export async function fetchCreators() {
  return request('/api/creators');
}

export async function fetchModels({
  search = '',
  designer = '',
  tag = '',
  month = '',
  bonus = false,
  hideCompleted = false,
  sort = '',
  limit = 24,
  offset = 0
} = {}) {
  const params = new URLSearchParams();
  if (search) params.set('search', search);
  if (designer) params.set('designer', designer);
  if (tag) params.set('tag', tag);
  if (month) params.set('month', month);
  if (bonus) params.set('bonus', '1');
  if (hideCompleted) params.set('hide_completed', '1');
  if (sort) params.set('sort', sort);
  params.set('limit', limit);
  params.set('offset', offset);
  return request(`/api/models?${params.toString()}`);
}

export async function fetchModelFiles(modelId) {
  return request(`/api/models/files?model_id=${encodeURIComponent(modelId)}`);
}

export async function fetchModelDetail(modelId) {
  return request(`/api/models/detail?id=${encodeURIComponent(modelId)}`);
}

// Archive Peeker & Submodels Inspection
export async function fetchArchiveSubmodels(fileId) {
  return request(`/api/models/archives?file_id=${encodeURIComponent(fileId)}`);
}

export async function fetchRawArchiveTree(fileId) {
  return request(`/api/models/archives/raw?file_id=${encodeURIComponent(fileId)}`);
}

export async function triggerArchivePeekBackfill(modelIds = null) {
  return request('/api/reconcile/archive-peek', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds }),
  });
}

export async function fetchSettings() {
  return request('/api/settings');
}

export async function updateSettings(payload) {
  return request('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function browseDirectory(path = '') {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  return request(`/api/system/browse?${params.toString()}`);
}

export async function fetchAuthStatus() {
  return request('/api/auth/status');
}

export async function saveAuthConfig(payload) {
  return request('/api/auth/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function getAuthLoginUrl() {
  return request('/api/auth/login?redirect=0');
}

export async function disconnectAuth() {
  return request('/api/auth/disconnect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function fetchTasksStatus() {
  return request('/api/tasks/status');
}

export async function cancelTask() {
  return request('/api/tasks/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function killTask() {
  return request('/api/tasks/kill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function clearTasksHistory() {
  return request('/api/tasks/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function startScraper() {
  return request('/api/scraper/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function fetchScrapesList() {
  return request('/api/scrapes/list');
}

export async function startIngest(payload = {}) {
  return request('/api/ingest/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// Vault Reconciliation
export async function runReconcileScan(scanMode = 'fast') {
  return request('/api/reconcile/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scan_mode: scanMode }),
  });
}

export async function fetchReconcileStatus() {
  return request('/api/reconcile/status');
}

export async function cancelReconcileScan() {
  return request('/api/reconcile/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function resetReconcileModels(modelIds) {
  return request('/api/reconcile/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds }),
  });
}

export async function deleteOrphanDirectories(paths) {
  return request('/api/reconcile/delete-orphans', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths }),
  });
}

export async function ingestUntrackedFiles(items) {
  return request('/api/reconcile/ingest-untracked', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  });
}

export async function fetchStagingGarbage() {
  return request('/api/reconcile/staging');
}

export async function purgeStagingGarbage(paths = null) {
  return request('/api/reconcile/staging/purge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths }),
  });
}

export async function fetchPathDrift() {
  return request('/api/reconcile/drift');
}

export async function realignModelPaths(modelIds = null) {
  return request('/api/reconcile/drift/realign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds }),
  });
}

export async function fetchDirectoryUsage() {
  const resp = await fetch('/api/settings/directories');
  if (!resp.ok) {
    throw new Error(`Failed to fetch directory usage: ${resp.statusText}`);
  }
  return await resp.json();
}

// -----------------------------------------------------------------------------
// Stash System ("A pile of gray") API Client
// -----------------------------------------------------------------------------

export async function fetchStashes() {
  return request('/api/stashes');
}

export async function createStash({ name, description = 'A pile of gray', make_active = false }) {
  return request('/api/stashes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description, make_active })
  });
}

export async function fetchActiveStash() {
  return request('/api/stashes/active');
}

export async function setActiveStash(stashId) {
  return request(`/api/stashes/${stashId}/active`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function fetchStashDetail(stashId) {
  return request(`/api/stashes/${stashId}`);
}

export async function deleteStash(stashId) {
  return request(`/api/stashes/${stashId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function addStashItem(stashId, { item_type, model_id, file_id = null, submodel_id = null, archive_member_id = null, notes = '' }) {
  const res = await fetch(`/api/stashes/${stashId}/items`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_type, model_id, file_id, submodel_id, archive_member_id, notes })
  });
  const data = await res.json();
  if (!res.ok && res.status !== 409) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export async function removeStashItem(stashItemId) {
  return request(`/api/stashes/items/${stashItemId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function bulkAddStashModels(stashId, modelIds) {
  return request(`/api/stashes/${stashId}/bulk-models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds })
  });
}

export async function triggerCatalogPreviewBackfill(modelIds = null) {
  return request('/api/reconcile/catalog-previews', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds }),
  });
}
