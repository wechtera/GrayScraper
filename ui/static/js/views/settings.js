import {
  fetchSettings,
  updateSettings,
  browseDirectory,
  fetchAuthStatus,
  saveAuthConfig,
  getAuthLoginUrl,
  disconnectAuth,
  fetchStorageStatus,
  fetchDirectoryUsage
} from '../api.js';
import { formatBytes, escapeHtml } from '../utils.js';

let currentBrowsePath = '';
let currentParentPath = '';
let activeBrowseTargetInputId = 'setting-root-path';
let toastTimeout = null;

function formatRemainingTime(expiresAt) {
  if (!expiresAt) return 'Unknown';
  const now = Math.floor(Date.now() / 1000);
  const diff = expiresAt - now;
  if (diff <= 0) return 'Expired';
  const hours = Math.floor(diff / 3600);
  const mins = Math.floor((diff % 3600) / 60);
  return hours > 0 ? `${hours}h ${mins}m remaining` : `${mins}m remaining`;
}

export async function loadDirectoryUsage() {
  const container = document.getElementById('critical-directories-table-container');
  if (!container) return;

  container.innerHTML = `
    <div class="loading-placeholder font-mono text-dim font-xs">
      Scanning disk usage across critical directories...
    </div>
  `;

  try {
    const data = await fetchDirectoryUsage();
    const dirs = data.directories || [];

    if (dirs.length === 0) {
      container.innerHTML = '<div class="text-dim font-xs">No directory configurations found.</div>';
      return;
    }

    const rowsHtml = dirs.map(d => {
      const statusBadge = d.exists
        ? '<span class="status-badge status-completed font-xs">Accessible</span>'
        : '<span class="status-badge status-not_downloaded font-xs">Not Found</span>';

      const volumeFreeLabel = d.exists && d.volume_total_bytes > 0
        ? `${formatBytes(d.volume_free_bytes)} free of ${formatBytes(d.volume_total_bytes)}`
        : '<span class="text-dim">-</span>';

      return `
        <tr>
          <td>
            <div class="font-semibold text-main">${escapeHtml(d.name)}</div>
            <div class="text-dim font-xs">${escapeHtml(d.description)}</div>
            <div class="font-mono text-dim font-xs mt-1" style="word-break: break-all;">
              ${escapeHtml(d.path)}
            </div>
          </td>
          <td>${statusBadge}</td>
          <td class="font-mono font-sm text-right">${d.exists ? d.file_count.toLocaleString() : '-'}</td>
          <td class="font-mono font-sm font-semibold text-right">${d.exists ? formatBytes(d.size_bytes) : '-'}</td>
          <td class="font-mono text-dim font-xs text-right">${volumeFreeLabel}</td>
        </tr>
      `;
    }).join('');

    container.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Directory / Description</th>
            <th style="width: 110px;">Status</th>
            <th style="width: 100px; text-align: right;">Files</th>
            <th style="width: 120px; text-align: right;">Disk Usage</th>
            <th style="width: 190px; text-align: right;">Volume Free Space</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    `;
  } catch (err) {
    container.innerHTML = `
      <div class="text-alert font-mono font-xs">
        Failed to fetch directory statistics: ${escapeHtml(err.message)}
      </div>
    `;
  }
}

// Global hook for manual refresh button
window.refreshDirectoryUsage = () => {
  loadDirectoryUsage();
};

export async function loadSettingsView() {
  try {
    const [settingsData, authData, storageStatus] = await Promise.all([
      fetchSettings(),
      fetchAuthStatus(),
      fetchStorageStatus()
    ]);

    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.value = val;
    };

    setVal('setting-root-path', settingsData.download_root_path || '');
    setVal('setting-work-path', settingsData.download_work_path || '');
    setVal('setting-concurrency', settingsData.worker_concurrency || 2);
    setVal('setting-offloader-concurrency', settingsData.offloader_concurrency || 1);
    setVal('setting-batch-size', settingsData.batch_size || 5);
    setVal('setting-cooldown', settingsData.batch_cooldown_seconds || 3.0);
    setVal('setting-stream-chunk', settingsData.stream_chunk_size_mb || 1.0);
    setVal('setting-offload-chunk', settingsData.offload_chunk_size_mb || 4.0);

    const nasMetricEl = document.getElementById('nas-disk-metric');
    if (nasMetricEl && storageStatus.nas) {
      if (storageStatus.nas.mounted) {
        nasMetricEl.textContent = `Free: ${formatBytes(storageStatus.nas.free_bytes)} / ${formatBytes(storageStatus.nas.total_bytes)}`;
      } else {
        nasMetricEl.textContent = `Status: ${storageStatus.nas.error || 'Unmounted / Offline'}`;
      }
    }

    const localMetricEl = document.getElementById('local-disk-metric');
    if (localMetricEl && storageStatus.local) {
      if (storageStatus.local.mounted) {
        localMetricEl.textContent = `Free: ${formatBytes(storageStatus.local.free_bytes)} / ${formatBytes(storageStatus.local.total_bytes)}`;
      } else {
        localMetricEl.textContent = `Status: ${storageStatus.local.error || 'Unconfigured / Offline'}`;
      }
    }

    const cookieVals = (settingsData.cookies && settingsData.cookies.values) || {};
    setVal('setting-cookie-cf', cookieVals.cf_clearance || '');
    setVal('setting-cookie-php', cookieVals.PHPSESSID || '');
    setVal('setting-cookies-raw', (settingsData.cookies && settingsData.cookies.raw_json) || '');

    setVal('setting-auth-client-id', authData.client_id || '');
    setVal('setting-auth-api-key', authData.client_secret || '');

    const apiKeyInput = document.getElementById('setting-auth-api-key');
    const toggleBtn = document.getElementById('btn-toggle-api-key');
    if (apiKeyInput && toggleBtn) {
      apiKeyInput.type = 'password';
      toggleBtn.textContent = 'Show';
    }

    const authBadge = document.getElementById('auth-status-badge');
    const authDiagnostics = document.getElementById('auth-diagnostics');
    const btnConnect = document.getElementById('btn-connect-auth');
    const btnDisconnect = document.getElementById('btn-disconnect-auth');
    const btnTest = document.getElementById('btn-test-auth');

    const isConnected = authData.connected && !authData.is_expired;

    if (authBadge) {
      authBadge.className = isConnected ? 'status-badge status-completed' : 'status-badge status-not_downloaded';
      authBadge.textContent = isConnected ? `Connected as @${authData.username}` : 'Not Connected';
    }

    if (btnConnect) btnConnect.textContent = isConnected ? 'Re-authenticate' : 'Connect Account';
    if (btnDisconnect) btnDisconnect.style.display = isConnected ? 'inline-flex' : 'none';
    if (btnTest) btnTest.style.display = isConnected ? 'inline-flex' : 'none';
    if (authDiagnostics) authDiagnostics.style.display = isConnected ? 'flex' : 'none';

    if (isConnected) {
      const setTxt = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
      };
      setTxt('diag-auth-user', `@${authData.username} (#${authData.user_id})`);
      setTxt('diag-auth-expiry', formatRemainingTime(authData.expires_at));
      setTxt('diag-auth-status', 'Active (Implicit Grant)');
    }

    // Trigger directory scan only upon loading the settings view
    loadDirectoryUsage();
  } catch (err) {
    console.error('Failed to load settings view:', err);
  }
}

export function openDirectoryPicker(targetInputId = 'setting-root-path') {
  activeBrowseTargetInputId = targetInputId;
  const targetEl = document.getElementById(targetInputId);
  const currentVal = targetEl ? targetEl.value.trim() : '';
  const modal = document.getElementById('browse-modal');
  if (modal) modal.style.display = 'flex';
  navigateBrowseFolder(currentVal);
}

export async function navigateBrowseFolder(path = '') {
  const pathDisplay = document.getElementById('browse-current-path');
  const dirList = document.getElementById('browse-dir-list');

  if (pathDisplay) pathDisplay.textContent = 'Scanning directory...';
  if (dirList) dirList.innerHTML = '<li class="browse-item browse-item-muted">Loading folders...</li>';

  try {
    const res = await browseDirectory(path);
    currentBrowsePath = res.current_path;
    currentParentPath = res.parent_path;

    if (pathDisplay) pathDisplay.textContent = currentBrowsePath;

    let itemsHtml = '';
    if (currentParentPath && currentParentPath !== currentBrowsePath) {
      itemsHtml += `
        <li class="browse-item" onclick="window.navigateBrowseFolder('${currentParentPath.replace(/'/g, "\\'")}')">
          <span class="font-mono text-dim font-xs">[DIR]</span>
          <strong>.. (Parent Directory)</strong>
        </li>
      `;
    }

    if (!res.directories || res.directories.length === 0) {
      itemsHtml += '<li class="browse-item browse-item-muted">No subdirectories found.</li>';
    } else {
      itemsHtml += res.directories.map(d => {
        const fullChild = currentBrowsePath.endsWith('/') || currentBrowsePath.endsWith('\\')
          ? `${currentBrowsePath}${d}`
          : `${currentBrowsePath}/${d}`;
        return `
          <li class="browse-item" onclick="window.navigateBrowseFolder('${fullChild.replace(/'/g, "\\'")}')">
            <span class="font-mono text-dim font-xs">[DIR]</span>
            <span>${d}</span>
          </li>
        `;
      }).join('');
    }

    if (dirList) dirList.innerHTML = itemsHtml;
  } catch (err) {
    if (pathDisplay) pathDisplay.textContent = 'Error accessing path';
    if (dirList) dirList.innerHTML = `<li class="browse-item browse-item-alert">${err.message}</li>`;
  }
}

export function selectCurrentBrowseFolder() {
  if (currentBrowsePath) {
    const targetEl = document.getElementById(activeBrowseTargetInputId);
    if (targetEl) {
      targetEl.value = currentBrowsePath;
      targetEl.dispatchEvent(new Event('input'));
    }
  }
  window.closeModal('browse-modal');
}

export async function saveSettingsForm() {
  const getVal = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };

  const payload = {
    stream_chunk_size_mb: parseFloat(getVal('setting-stream-chunk') || '1.0'),
    offload_chunk_size_mb: parseFloat(getVal('setting-offload-chunk') || '4.0'),
    download_root_path: getVal('setting-root-path'),
    download_work_path: getVal('setting-work-path'),
    worker_concurrency: parseInt(getVal('setting-concurrency') || '2', 10),
    offloader_concurrency: parseInt(getVal('setting-offloader-concurrency') || '1', 10),
    batch_size: parseInt(getVal('setting-batch-size') || '5', 10),
    batch_cooldown_seconds: parseFloat(getVal('setting-cooldown') || '3.0'),
    cookies: {
      cf_clearance: getVal('setting-cookie-cf'),
      PHPSESSID: getVal('setting-cookie-php')
    },
    raw_cookies_json: getVal('setting-cookies-raw')
  };

  try {
    const res = await updateSettings(payload);
    showSettingsToast(`Settings updated successfully at ${res.updated_at.split(' ')[1] || res.updated_at}`);
    window.checkHealth();
    loadSettingsView();
  } catch (err) {
    alert(`Failed to save settings: ${err.message}`);
  }
}

export function toggleApiKeyVisibility() {
  const input = document.getElementById('setting-auth-api-key');
  const btn = document.getElementById('btn-toggle-api-key');
  if (!input) return;

  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = 'Hide';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = 'Show';
  }
}

export async function saveAuthCredentials() {
  const clientEl = document.getElementById('setting-auth-client-id');
  const apiEl = document.getElementById('setting-auth-api-key');
  const clientId = clientEl ? clientEl.value.trim() : '';
  const apiKey = apiEl ? apiEl.value.trim() : '';

  if (!clientId && !apiKey) return alert('Please enter your application slug or API key.');

  try {
    await saveAuthConfig({ client_id: clientId, client_secret: apiKey });
    showSettingsToast('Authentication credentials saved.');
    loadSettingsView();
  } catch (err) {
    alert(`Failed to save credentials: ${err.message}`);
  }
}

export const saveAuthSlug = saveAuthCredentials;

export async function connectAccount() {
  const clientEl = document.getElementById('setting-auth-client-id');
  const apiEl = document.getElementById('setting-auth-api-key');
  const clientId = clientEl ? clientEl.value.trim() : '';
  const apiKey = apiEl ? apiEl.value.trim() : '';

  if (!clientId) return alert('Please specify your application slug first.');
  try {
    await saveAuthConfig({ client_id: clientId, client_secret: apiKey });
    const res = await getAuthLoginUrl();
    if (res.auth_url) {
      window.location.href = res.auth_url;
    }
  } catch (err) {
    alert(`Could not initiate authorization: ${err.message}`);
  }
}

export async function disconnectAccount() {
  if (!confirm('Disconnect your MyMiniFactory account? Local access tokens will be removed.')) return;
  try {
    await disconnectAuth();
    showSettingsToast('MyMiniFactory account disconnected.');
    loadSettingsView();
  } catch (err) {
    alert(`Disconnection failed: ${err.message}`);
  }
}

export async function testAccountConnection() {
  try {
    const status = await fetchAuthStatus();
    if (status.connected && !status.is_expired) {
      showSettingsToast(`Token valid! Connected as @${status.username} (ID: ${status.user_id})`);
    } else {
      showSettingsToast('Token is expired or invalid. Please re-authenticate.');
    }
    loadSettingsView();
  } catch (err) {
    alert(`Status check failed: ${err.message}`);
  }
}

export function showSettingsToast(msg) {
  const toast = document.getElementById('settings-toast');
  if (!toast) return;

  toast.textContent = msg;
  toast.classList.add('show');

  if (toastTimeout) clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => {
    toast.classList.remove('show');
  }, 4000);
}
