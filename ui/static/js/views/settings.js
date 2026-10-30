/**
 * ui/static/js/views/settings.js - Settings workspace controller.
 * Manages storage configuration, directory usage metrics, in-place external libraries,
 * worker tuning, diagnostic logging, log pruning, and MyMiniFactory authentication.
 */

import { formatBytes, escapeHtml } from '../utils.js';
import {
  fetchExternalLibraries,
  addExternalLibrary,
  deleteExternalLibrary,
  scanExternalLibrary
} from '../api.js';

let currentBrowsePath = '';
let targetInputId = '';

function showToast(message) {
  const toast = document.getElementById('settings-toast');
  if (toast) {
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 3500);
  } else {
    console.log('[Toast]', message);
  }
}

/**
 * Parses a Cookie-Editor array or key-value object and extracts cf_clearance and PHPSESSID.
 */
function extractTokensFromCookiesJson(jsonStr) {
  if (!jsonStr || typeof jsonStr !== 'string') return { cf: '', php: '' };
  try {
    const parsed = JSON.parse(jsonStr.trim());
    let cf = '';
    let php = '';
    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        if (!item || typeof item !== 'object') continue;
        const name = (item.name || '').trim();
        const value = (item.value || '').trim();
        if (name === 'cf_clearance') cf = value;
        if (name === 'PHPSESSID' || name.toLowerCase() === 'phpsessid') php = value;
      }
    } else if (typeof parsed === 'object') {
      for (const [k, v] of Object.entries(parsed)) {
        if (k === 'cf_clearance') cf = String(v).trim();
        if (k === 'PHPSESSID' || k.toLowerCase() === 'phpsessid') php = String(v).trim();
      }
    }
    return { cf, php };
  } catch (e) {
    return { cf: '', php: '' };
  }
}

function bindCookieJsonSync() {
  const rawTextarea = document.getElementById('setting-cookies-raw');
  if (rawTextarea && !rawTextarea.dataset.syncBound) {
    rawTextarea.dataset.syncBound = 'true';
    const syncHandler = () => {
      const { cf, php } = extractTokensFromCookiesJson(rawTextarea.value);
      const cfInput = document.getElementById('setting-cookie-cf');
      const phpInput = document.getElementById('setting-cookie-php');
      if (cf && cfInput) cfInput.value = cf;
      if (php && phpInput) phpInput.value = php;
    };
    rawTextarea.addEventListener('input', syncHandler);
    rawTextarea.addEventListener('paste', () => setTimeout(syncHandler, 50));
  }
}

export async function loadSettingsView() {
  await Promise.all([
    loadSettings(),
    loadDirectoryUsage(),
    loadExternalLibraries(),
    loadAuthStatus(),
    loadLoggingSettings()
  ]);
  bindCookieJsonSync();
}

export async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();

    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el && val !== undefined && val !== null) el.value = val;
    };

    setVal('setting-stash-export-path', data.stash_download_path || '~/Downloads/DESYNC_Print_Queue');
    setVal('setting-root-path', data.download_root_path || '');
    setVal('setting-work-path', data.download_work_path || '');
    setVal('setting-concurrency', data.worker_concurrency || 2);
    setVal('setting-offloader-concurrency', data.offloader_concurrency || 1);
    setVal('setting-batch-size', data.batch_size || 5);
    setVal('setting-cooldown', data.batch_cooldown_seconds || 3.0);
    setVal('setting-stream-chunk', data.stream_chunk_size_mb || 1.0);
    setVal('setting-offload-chunk', data.offload_chunk_size_mb || 4.0);

    const rawJson = data.cookies_json || '';
    setVal('setting-cookies-raw', rawJson);

    let cf = data.cookie_cf_clearance || '';
    let php = data.cookie_phpsessid || '';

    // Auto-extract from raw JSON if individual fields are empty
    if ((!cf || !php) && rawJson) {
      const extracted = extractTokensFromCookiesJson(rawJson);
      if (!cf) cf = extracted.cf;
      if (!php) php = extracted.php;
    }

    setVal('setting-cookie-cf', cf);
    setVal('setting-cookie-php', php);

    setVal('setting-auth-client-id', data.mmf_client_id || '');
    setVal('setting-auth-api-key', data.mmf_client_secret || '');
  } catch (err) {
    console.error('Failed to load settings:', err);
  }
}

export async function loadDirectoryUsage() {
  const container = document.getElementById('critical-directories-table-container');
  if (!container) return;

  container.innerHTML = '<div class="table-msg">Scanning directory footprints...</div>';

  try {
    const res = await fetch('/api/settings/directories');
    const data = await res.json();
    const directories = data.directories || data.items || [];

    if (!directories.length) {
      container.innerHTML = '<div class="table-msg">No directory metrics available.</div>';
      return;
    }

    container.innerHTML = `
      <table class="table-compact">
        <thead>
          <tr>
            <th>Directory Scope</th>
            <th>Resolved Server Path</th>
            <th>Item Count</th>
            <th>Disk Footprint</th>
          </tr>
        </thead>
        <tbody>
          ${directories.map(d => `
            <tr>
              <td><strong>${escapeHtml(d.name)}</strong></td>
              <td class="font-mono font-xs text-dim">${escapeHtml(d.path || '-')}</td>
              <td>${d.file_count !== undefined ? d.file_count.toLocaleString() : '-'}</td>
              <td class="font-mono">${d.size_bytes !== null && d.size_bytes !== undefined ? formatBytes(d.size_bytes) : '<span class="text-dim">N/A (Database)</span>'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  } catch (err) {
    container.innerHTML = `<div class="table-msg text-alert">Failed scanning directories: ${escapeHtml(err.message)}</div>`;
  }
}

// -----------------------------------------------------------------------------
// In-Place External Libraries Management (Phase 9)
// -----------------------------------------------------------------------------

export async function loadExternalLibraries() {
  let panel = document.getElementById('external-libraries-panel');

  // Auto-scaffold panel into settings-grid if not already in index.html
  if (!panel) {
    const grid = document.querySelector('.settings-grid');
    if (grid) {
      panel = document.createElement('div');
      panel.id = 'external-libraries-panel';
      panel.className = 'settings-panel';
      if (grid.children.length > 1) {
        grid.insertBefore(panel, grid.children[1]);
      } else {
        grid.appendChild(panel);
      }
    }
  }

  if (!panel) return;

  panel.innerHTML = `
    <div class="settings-panel-header">
      <div>
        <h2 class="settings-panel-title">In-Place External Libraries</h2>
        <div class="settings-panel-subtitle">
          Manage local &amp; network storage collections (e.g., Loot Studios) indexed zero-copy in strictly read-only mode.
        </div>
      </div>
      <div class="flex gap-2">
        <button type="button" class="btn-pill" onclick="window.loadExternalLibraries()">Refresh Libraries</button>
      </div>
    </div>
    <div id="external-libraries-table-container">
      <div class="table-msg">Loading registered libraries...</div>
    </div>
    <div class="mt-4 pt-3" style="border-top: 1px solid #262635;">
      <h3 class="font-bold font-md mb-2 text-accent">Register New In-Place Library</h3>
      <div class="settings-form-row two-col">
        <div class="settings-field-group">
          <label for="new-lib-path">Top-Level Storage Directory Path *</label>
          <div class="settings-input-with-button">
            <input type="text" id="new-lib-path" placeholder="/Volumes/STL/Loot Studios" spellcheck="false">
            <button type="button" class="btn-pill" onclick="window.openDirectoryPicker('new-lib-path')">Browse</button>
          </div>
          <span class="form-help-text">Direct path to library root on NAS or local drive (strictly read-only).</span>
        </div>
        <div class="settings-field-group">
          <label for="new-lib-name">Display Name (Optional)</label>
          <input type="text" id="new-lib-name" placeholder="Defaults to folder name" spellcheck="false">
          <span class="form-help-text">Label shown in catalog and monthly releases.</span>
        </div>
      </div>
      <div class="settings-form-row two-col mt-2">
        <div class="settings-field-group">
          <label for="new-lib-creator">Default Creator / Studio (Optional)</label>
          <input type="text" id="new-lib-creator" placeholder="e.g. Loot Studios" spellcheck="false">
          <span class="form-help-text">Assigned to indexed models if not specified in release subfolders.</span>
        </div>
        <div class="settings-field-group flex items-end">
          <button type="button" class="btn-pill btn-action-primary" onclick="window.handleAddExternalLibrary()">
            Register In-Place Library
          </button>
        </div>
      </div>
    </div>
  `;

  const tableContainer = document.getElementById('external-libraries-table-container');
  try {
    const res = await fetchExternalLibraries();
    const libs = res.libraries || [];

    if (!libs.length) {
      tableContainer.innerHTML = `
        <div class="table-msg text-dim py-4">
          No external libraries registered. Register a directory above to index external collections without moving files.
        </div>
      `;
      return;
    }

    tableContainer.innerHTML = `
      <table class="table-compact">
        <thead>
          <tr>
            <th>Name / Studio</th>
            <th>Storage Path</th>
            <th>Disk Footprint</th>
            <th>Models</th>
            <th>Total Files</th>
            <th>Last Scanned</th>
            <th style="width: 175px;">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${libs.map(lib => {
            const lastScan = lib.last_scanned_at
              ? new Date(lib.last_scanned_at).toLocaleString()
              : '<span class="text-dim">Never Scanned</span>';
            const existsBadge = lib.exists
              ? ''
              : '<span class="status-badge status-danger font-xs ml-1" title="Directory path not currently mounted">Unmounted</span>';

            return `
              <tr>
                <td>
                  <strong>${escapeHtml(lib.name)}</strong>
                  <div class="text-dim font-xs">${escapeHtml(lib.default_creator || lib.name)}</div>
                </td>
                <td class="font-mono font-xs text-dim">
                  ${escapeHtml(lib.path)} ${existsBadge}
                </td>
                <td class="font-mono">${escapeHtml(lib.disk_formatted || '0 B')}</td>
                <td class="font-mono">${(lib.total_models || 0).toLocaleString()}</td>
                <td class="font-mono">${(lib.total_files || 0).toLocaleString()}</td>
                <td class="font-sm">${lastScan}</td>
                <td>
                  <div class="flex gap-1">
                    <button type="button" class="btn-pill font-xs btn-action-primary" onclick="window.handleScanExternalLibrary(${lib.id}, '${escapeHtml(lib.name).replace(/'/g, "\\'")}')" title="Scan and reconcile external models into library">
                      Scan Library
                    </button>
                    <button type="button" class="btn-pill font-xs btn-action-cancel" onclick="window.handleDeleteExternalLibrary(${lib.id}, '${escapeHtml(lib.name).replace(/'/g, "\\'")}')" title="Unlink library from database (preserves source NAS files)">
                      Remove
                    </button>
                  </div>
                </td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  } catch (err) {
    tableContainer.innerHTML = `<div class="table-msg text-alert">Failed loading external libraries: ${escapeHtml(err.message)}</div>`;
  }
}

export async function handleAddExternalLibrary(event) {
  if (event) event.preventDefault();
  const pathInput = document.getElementById('new-lib-path');
  const nameInput = document.getElementById('new-lib-name');
  const creatorInput = document.getElementById('new-lib-creator');

  const path = pathInput ? pathInput.value.trim() : '';
  const name = nameInput ? nameInput.value.trim() : '';
  const default_creator = creatorInput ? creatorInput.value.trim() : '';

  if (!path) {
    alert('Please specify a directory path for the in-place library.');
    return;
  }

  try {
    const res = await addExternalLibrary({ path, name, default_creator });
    if (res.success) {
      showToast(res.message || `In-place library '${res.library.name}' registered.`);
      if (pathInput) pathInput.value = '';
      if (nameInput) nameInput.value = '';
      if (creatorInput) creatorInput.value = '';
      await loadExternalLibraries();
    } else {
      alert(`Failed to add library: ${res.error}`);
    }
  } catch (err) {
    alert(`Registration error: ${err.message}`);
  }
}

export async function handleScanExternalLibrary(libraryId, libraryName) {
  try {
    const res = await scanExternalLibrary(libraryId);
    if (res.success) {
      showToast(`Scan started for '${libraryName}' (Task #${res.task_id}).`);
      if (window.refreshTasksStatus) {
        window.refreshTasksStatus();
      }
    } else {
      alert(`Failed to start scan: ${res.error}`);
    }
  } catch (err) {
    alert(`Scan error: ${err.message}`);
  }
}

export async function handleDeleteExternalLibrary(libraryId, libraryName) {
  if (!confirm(`Are you sure you want to unlink '${libraryName}'?\n\nNOTE: No physical files or directories on your storage will be touched or deleted.`)) {
    return;
  }
  try {
    const res = await deleteExternalLibrary(libraryId);
    if (res.success) {
      showToast(res.message || `Library '${libraryName}' unlinked.`);
      await loadExternalLibraries();
    } else {
      alert(`Failed to remove library: ${res.error}`);
    }
  } catch (err) {
    alert(`Remove error: ${err.message}`);
  }
}

// -----------------------------------------------------------------------------
// Native MMF Authentication & Diagnostics
// -----------------------------------------------------------------------------

export async function loadAuthStatus() {
  try {
    const res = await fetch('/api/auth/status');
    const data = await res.json();

    const badge = document.getElementById('auth-status-badge');
    const diagBox = document.getElementById('auth-diagnostics');
    const btnConnect = document.getElementById('btn-connect-auth');
    const btnTest = document.getElementById('btn-test-auth');
    const btnDisconnect = document.getElementById('btn-disconnect-auth');

    if (!badge) return;

    if (data.connected) {
      badge.textContent = `CONNECTED AS @${(data.username || 'USER').toUpperCase()}`;
      badge.className = 'status-badge status-completed font-xs';

      if (diagBox) diagBox.style.display = 'grid';
      const diagUser = document.getElementById('diag-auth-user');
      const diagExpiry = document.getElementById('diag-auth-expiry');
      const diagStatus = document.getElementById('diag-auth-status');

      if (diagUser) diagUser.textContent = data.username ? `@${data.username} (ID: ${data.user_id})` : '-';
      if (diagExpiry) {
        if (data.expires_at) {
          const expDate = new Date(data.expires_at * 1000);
          diagExpiry.textContent = expDate.toLocaleString();
        } else {
          diagExpiry.textContent = 'Indefinite / Permanent';
        }
      }
      if (diagStatus) diagStatus.textContent = 'OAuth 2.0 Bearer Authenticated';

      if (btnConnect) btnConnect.style.display = 'none';
      if (btnTest) btnTest.style.display = 'inline-block';
      if (btnDisconnect) btnDisconnect.style.display = 'inline-block';
    } else {
      badge.textContent = data.configured ? 'DISCONNECTED / EXPIRED' : 'NOT CONFIGURED';
      badge.className = 'status-badge status-not_downloaded font-xs';

      if (diagBox) diagBox.style.display = 'none';
      if (btnConnect) btnConnect.style.display = 'inline-block';
      if (btnTest) btnTest.style.display = 'none';
      if (btnDisconnect) btnDisconnect.style.display = 'none';
    }
  } catch (err) {
    console.error('Failed to load auth status:', err);
  }
}

export async function loadLoggingSettings() {
  try {
    const res = await fetch('/api/settings/logging');
    const data = await res.json();
    if (!data.success) return;

    const config = data.config || {};
    const levelSelect = document.getElementById('setting-log-level');
    if (levelSelect && config.level) {
      levelSelect.value = config.level;
    }

    const activeLayers = new Set(config.layers || []);
    ['http', 'storage', 'archive', 'sql'].forEach(layer => {
      const chk = document.getElementById(`layer-${layer}`);
      if (chk) {
        chk.checked = activeLayers.has(layer);
      }
    });
  } catch (err) {
    console.error('Failed to load logging settings:', err);
  }
}

export async function saveLoggingSettings() {
  const levelSelect = document.getElementById('setting-log-level');
  const level = levelSelect ? levelSelect.value : 'INFO';

  const layers = [];
  ['http', 'storage', 'archive', 'sql'].forEach(layer => {
    const chk = document.getElementById(`layer-${layer}`);
    if (chk && chk.checked) {
      layers.push(layer);
    }
  });

  try {
    const res = await fetch('/api/settings/logging', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level, layers })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Logging configuration applied.');
    } else {
      alert(`Failed to save logging configuration: ${data.error}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

export async function purgeLogs(days = 14) {
  if (!confirm(`Are you sure you want to delete rotated log files and audit records older than ${days} days?`)) {
    return;
  }
  try {
    const res = await fetch('/api/settings/logs/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message);
      loadDirectoryUsage();
    } else {
      alert(`Log purge failed: ${data.error}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

export async function saveSettingsForm() {
  const getVal = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };

  let cfVal = getVal('setting-cookie-cf');
  let phpVal = getVal('setting-cookie-php');
  const rawJson = getVal('setting-cookies-raw');

  if ((!cfVal || !phpVal) && rawJson) {
    const extracted = extractTokensFromCookiesJson(rawJson);
    if (!cfVal) cfVal = extracted.cf;
    if (!phpVal) phpVal = extracted.php;
  }

  let cleanJson = rawJson;
  if (rawJson) {
    try {
      const parsed = JSON.parse(rawJson);
      cleanJson = JSON.stringify(parsed);
    } catch (e) {
      return alert('Raw Browser Cookies JSON is not valid JSON format.');
    }
  } else if (cfVal || phpVal) {
    const synth = [];
    if (cfVal) synth.push({ domain: ".myminifactory.com", name: "cf_clearance", path: "/", value: cfVal });
    if (phpVal) synth.push({ domain: ".myminifactory.com", name: "PHPSESSID", path: "/", value: phpVal });
    cleanJson = JSON.stringify(synth);
    const rawEl = document.getElementById('setting-cookies-raw');
    if (rawEl) rawEl.value = JSON.stringify(synth, null, 2);
  }

  const cfEl = document.getElementById('setting-cookie-cf');
  if (cfEl && cfVal) cfEl.value = cfVal;
  const phpEl = document.getElementById('setting-cookie-php');
  if (phpEl && phpVal) phpEl.value = phpVal;

  const payload = {
    download_root_path: getVal('setting-root-path'),
    download_work_path: getVal('setting-work-path'),
    stash_download_path: getVal('setting-stash-export-path') || '~/Downloads/DESYNC_Print_Queue',
    worker_concurrency: getVal('setting-concurrency') || 2,
    offloader_concurrency: getVal('setting-offloader-concurrency') || 1,
    batch_size: getVal('setting-batch-size') || 5,
    batch_cooldown_seconds: getVal('setting-cooldown') || 3.0,
    stream_chunk_size_mb: getVal('setting-stream-chunk') || 1.0,
    offload_chunk_size_mb: getVal('setting-offload-chunk') || 4.0,
    cookie_cf_clearance: cfVal,
    cookie_phpsessid: phpVal,
    cookies_json: cleanJson,
  };

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success || res.ok) {
      showToast('Settings saved successfully.');
      loadSettings();
      loadDirectoryUsage();
    } else {
      alert(`Failed saving settings: ${data.error || 'Server error'}`);
    }
  } catch (err) {
    alert(`Failed saving settings: ${err.message}`);
  }
}

export async function saveAuthCredentials() {
  const clientId = (document.getElementById('setting-auth-client-id')?.value || '').trim();
  const apiKey = (document.getElementById('setting-auth-api-key')?.value || '').trim();

  try {
    const res = await fetch('/api/auth/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, client_secret: apiKey })
    });
    const data = await res.json();
    if (data.success) {
      showToast('OAuth credentials saved.');
      loadAuthStatus();
    } else {
      alert(`Failed to save credentials: ${data.error}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

export function saveAuthSlug() {
  return saveAuthCredentials();
}

export async function connectAccount() {
  try {
    const res = await fetch('/api/auth/login');
    const data = await res.json();
    if (data.login_url) {
      window.location.href = data.login_url;
    } else {
      alert(data.error || 'Failed to generate MyMiniFactory login authorization URL.');
    }
  } catch (err) {
    alert(`Connection error: ${err.message}`);
  }
}

export async function disconnectAccount() {
  if (!confirm('Disconnect this MyMiniFactory account?')) return;
  try {
    const res = await fetch('/api/auth/disconnect', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('Account disconnected.');
      loadAuthStatus();
    }
  } catch (err) {
    alert(`Disconnect error: ${err.message}`);
  }
}

export async function testAccountConnection() {
  try {
    const res = await fetch('/api/auth/status');
    const data = await res.json();
    if (data.connected) {
      alert(`Connection active! Authenticated as @${data.username} (ID: ${data.user_id}).`);
    } else {
      alert('Connection expired or not connected.');
    }
  } catch (err) {
    alert(`Test error: ${err.message}`);
  }
}

export function toggleApiKeyVisibility() {
  const el = document.getElementById('setting-auth-api-key');
  const btn = document.getElementById('btn-toggle-api-key');
  if (el && btn) {
    if (el.type === 'password') {
      el.type = 'text';
      btn.textContent = 'Hide';
    } else {
      el.type = 'password';
      btn.textContent = 'Show';
    }
  }
}

export async function openDirectoryPicker(inputId) {
  targetInputId = inputId;
  const initialPath = document.getElementById(inputId)?.value.trim() || '';
  currentBrowsePath = initialPath;
  await navigateBrowseFolder(initialPath);
  const modal = document.getElementById('browse-modal');
  if (modal) modal.style.display = 'flex';
}

export async function navigateBrowseFolder(folderPath) {
  try {
    const url = `/api/system/browse${folderPath ? `?path=${encodeURIComponent(folderPath)}` : ''}`;
    const res = await fetch(url);
    const data = await res.json();

    currentBrowsePath = data.current_path || '';
    const pathBox = document.getElementById('browse-current-path');
    if (pathBox) pathBox.textContent = currentBrowsePath;

    const list = document.getElementById('browse-dir-list');
    if (!list) return;

    let html = '';
    if (data.parent_path) {
      html += `
        <li class="browse-item cursor-pointer" onclick="window.navigateBrowseFolder('${data.parent_path.replace(/'/g, "\\'")}')">
          <span class="text-dim font-mono">[..]</span> <span class="font-semibold text-accent">Parent Folder</span>
        </li>
      `;
    }

    const dirs = data.directories || [];
    if (!dirs.length) {
      html += `<li class="browse-item text-dim">No subdirectories found.</li>`;
    } else {
      dirs.forEach(d => {
        html += `
          <li class="browse-item cursor-pointer" onclick="window.navigateBrowseFolder('${d.path.replace(/'/g, "\\'")}')">
            <span class="text-accent font-mono">[DIR]</span> ${escapeHtml(d.name)}
          </li>
        `;
      });
    }
    list.innerHTML = html;
  } catch (err) {
    console.error('Directory browser navigation error:', err);
  }
}

export function selectCurrentBrowseFolder() {
  if (targetInputId && currentBrowsePath) {
    const input = document.getElementById(targetInputId);
    if (input) input.value = currentBrowsePath;
  }
  const modal = document.getElementById('browse-modal');
  if (modal) modal.style.display = 'none';
}

export const initSettingsView = loadSettingsView;
export const refreshDirectoryUsage = loadDirectoryUsage;

export async function purgeStashExports(days = 14) {
  if (!confirm(`Are you sure you want to delete print queue stash folders older than ${days} days?`)) {
    return;
  }
  try {
    const res = await fetch('/api/settings/stashes/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message);
      loadDirectoryUsage();
    } else {
      alert(`Stash purge failed: ${data.error || 'Server error'}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

// -----------------------------------------------------------------------------
// Window Bindings for Global DOM Event Handlers
// -----------------------------------------------------------------------------
Object.assign(window, {
  loadSettingsView,
  initSettingsView,
  loadSettings,
  refreshDirectoryUsage,
  loadDirectoryUsage,
  loadExternalLibraries,
  handleAddExternalLibrary,
  handleScanExternalLibrary,
  handleDeleteExternalLibrary,
  loadAuthStatus,
  loadLoggingSettings,
  saveLoggingSettings,
  purgeLogs,
  saveSettingsForm,
  saveAuthSlug,
  saveAuthCredentials,
  connectAccount,
  disconnectAccount,
  testAccountConnection,
  toggleApiKeyVisibility,
  openDirectoryPicker,
  navigateBrowseFolder,
  selectCurrentBrowseFolder,
  purgeStashExports,
});
