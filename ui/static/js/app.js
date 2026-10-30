import {
  fetchMetrics,
  fetchStorageStatus,
  saveStorageConfig as apiSaveStorageConfig,
  fetchDownloadsStatus,
  fetchActiveStash
} from './api.js';
import { state } from './state.js';
import { formatBytes } from './utils.js';

// --- View: Releases & Catalog Continuity ---
import {
  loadReleases,
  loadAudit,
  openReleaseModal,
  queueReleasePack,
  cancelReleaseQueue,
  resetReleasePageAndLoad,
  changeReleasePage
} from './views/releases.js';

// --- View: Models Explorer ---
import {
  loadModels,
  renderModelCard,
  openFilesModal,
  openModelDetailModal,
  resetModelPageAndLoad,
  changeModelPage,
  filterByTag,
  populateCreatorsFilter,
  openArchiveViewerModal,
  switchArchiveViewMode
} from './views/models.js';

// --- View: Creators Directory ---
import {
  loadCreators,
  filterByCreator
} from './views/creators.js';

// --- View: Downloads, Vault Reconciliation & Storage Maintenance ---
import {
  // Pending & Queue Explorer
  switchDownloadSubView,
  resetPendingPageAndLoad,
  changePendingPage,
  loadPendingModels,
  toggleSelectPending,
  toggleSelectAllPending,
  queueSelectedModels,
  dequeueSelectedModels,
  queueSingleModel,
  dequeueSingleModel,
  clearQueueStatus,
  confirmPurgeIncomplete,

  // Active Transfers & Queue Controls
  renderActiveTransfers,
  startActiveTransfersPolling,
  stopActiveTransfersPolling,
  controlDownload,

  // History & Audit Log
  resetHistoryPageAndLoad,
  changeHistoryPage,
  loadDownloadHistory,

  // Forward & Deep Reconciliation
  setReconcileMode,
  triggerReconcileScan,
  cancelReconcile,
  loadReconcileView,
  updateReconcileFromHealth,
  resetSingleReconcileModel,
  resetAllReconcileDiscrepancies,

  // Reverse Reconciliation (Orphans & Untracked Files)
  triggerOrphanAudit,
  deleteSingleOrphan,
  deleteAllOrphans,
  ingestSingleUntracked,
  ingestAllUntracked,

  // Staging Scratch Garbage Collection
  triggerStagingSweep,
  purgeSingleStagingGarbage,
  purgeAllStagingGarbage,

  // Path Drift & Canonical Re-alignment
  triggerPathDriftScan,
  realignSinglePath,
  realignAllPaths,

  // Archive Inspection
  triggerArchiveInspectionSweep
} from './views/downloads.js';

// --- View: Stashes ("A pile of gray") Workspace ---
import {
  loadStashesView,
  updateHeaderStashWidget,
  selectStash,
  handleCreateStash,
  handleSetActiveStash,
  handleDeleteStash,
  handleRemoveStashItem
} from './views/stashes.js';

// --- View: Settings & Native Authentication ---
import {
  loadSettingsView,
  openDirectoryPicker,
  navigateBrowseFolder,
  selectCurrentBrowseFolder,
  saveSettingsForm,
  saveAuthSlug,
  saveAuthCredentials,
  toggleApiKeyVisibility,
  connectAccount,
  disconnectAccount,
  testAccountConnection
} from './views/settings.js';

// --- View: Pipeline Tasks, Scraper & Ingest Automation ---
import {
  renderTasksView,
  openScraperModal,
  triggerLibraryScrape,
  openIngestModal,
  loadRecentScrapesForIngest,
  triggerMetadataIngest,
  renderIngestReport,
  cancelActiveTask,
  killActiveTask,
  clearTaskHistory
} from './views/tasks.js';

let healthPollTimer = null;
let isTaskRunning = false;

// -----------------------------------------------------------------------------
// Core Metrics & View Refresh
// -----------------------------------------------------------------------------

export async function loadMetrics() {
  try {
    const data = await fetchMetrics();
    const setTxt = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setTxt('stat-creators', (data.total_creators || 0).toLocaleString());
    setTxt('stat-models', (data.total_models || 0).toLocaleString());

    const dlModels = data.total_downloaded_models || 0;
    setTxt('stat-supported', dlModels > 0
      ? `${(data.supported_models || 0).toLocaleString()} Supported \u2022 ${dlModels.toLocaleString()} Archived`
      : `${(data.supported_models || 0).toLocaleString()} Pre-Supported`);

    setTxt('stat-bonuses', (data.total_bonus_models || 0).toLocaleString());
    setTxt('stat-files', (data.total_files || 0).toLocaleString());

    const totalLibBytes = data.total_library_size_bytes || data.total_size_bytes || 0;
    const dlBytes = data.total_downloaded_bytes || 0;
    const dlPct = totalLibBytes > 0 ? ((dlBytes / totalLibBytes) * 100).toFixed(1) : '0.0';

    setTxt('stat-size', formatBytes(totalLibBytes));
    setTxt('stat-download-progress', `Archived: ${formatBytes(dlBytes)} (${dlPct}%)`);
    setTxt('stat-releases', (data.total_releases || 0).toLocaleString());
    setTxt('stat-bundles', `${data.likely_bundles || 0} Likely Bundles`);
  } catch (err) {
    console.error('Failed to load metrics:', err);
  }
}

export function refreshCurrentView() {
  loadMetrics();
  const isVisible = id => {
    const el = document.getElementById(id);
    return el && el.style.display !== 'none';
  };

  if (isVisible('view-releases')) loadReleases();
  if (isVisible('view-models')) loadModels();
  if (isVisible('view-creators')) loadCreators();
  if (isVisible('view-downloads')) {
    if (isVisible('sub-dl-pending')) loadPendingModels();
    if (isVisible('sub-dl-active')) {
      renderActiveTransfers();
      startActiveTransfersPolling();
    }
    if (isVisible('sub-dl-history')) loadDownloadHistory();
    if (isVisible('sub-dl-reconcile')) loadReconcileView();
  }
  if (isVisible('view-stashes')) loadStashesView();
  if (isVisible('view-settings')) {
    loadSettingsView();
    renderTasksView();
  }
}

export function switchTab(tabName) {
  // If user requests legacy #tasks, redirect to settings and scroll to tasks
  let shouldScrollToTasks = false;
  if (tabName === 'tasks') {
    tabName = 'settings';
    shouldScrollToTasks = true;
  }

  const allViews = ['releases', 'models', 'creators', 'downloads', 'stashes', 'settings', 'help'];
  allViews.forEach(v => {
    const el = document.getElementById('view-' + v);
    if (el) el.style.display = (v === tabName) ? 'block' : 'none';
  });

  document.querySelectorAll('.nav-tabs .tab-btn').forEach(btn => btn.classList.remove('active'));
  const activeBtn = document.getElementById('nav-tab-' + tabName);
  if (activeBtn) activeBtn.classList.add('active');

  const libraryBtn = document.getElementById('btn-library-dropdown');
  if (libraryBtn) {
    if (['releases', 'models', 'creators'].includes(tabName)) {
      libraryBtn.classList.add('active');
    } else {
      libraryBtn.classList.remove('active');
    }
  }

  const isVisible = id => {
    const el = document.getElementById(id);
    return el && el.style.display !== 'none';
  };

  if (tabName === 'releases') {
    stopActiveTransfersPolling();
    loadReleases();
  } else if (tabName === 'models') {
    stopActiveTransfersPolling();
    loadModels();
  } else if (tabName === 'creators') {
    stopActiveTransfersPolling();
    loadCreators();
  } else if (tabName === 'downloads') {
    if (isVisible('sub-dl-active')) {
      renderActiveTransfers();
      startActiveTransfersPolling();
    } else if (isVisible('sub-dl-history')) {
      stopActiveTransfersPolling();
      loadDownloadHistory();
    } else if (isVisible('sub-dl-reconcile')) {
      stopActiveTransfersPolling();
      loadReconcileView();
    } else {
      stopActiveTransfersPolling();
      loadPendingModels();
    }
  } else if (tabName === 'stashes') {
    stopActiveTransfersPolling();
    loadStashesView();
  } else if (tabName === 'settings') {
    stopActiveTransfersPolling();
    loadSettingsView();
    renderTasksView();
    if (shouldScrollToTasks) {
      setTimeout(() => {
        const tasksSection = document.getElementById('settings-pipeline-tasks') || document.getElementById('active-task-container');
        if (tasksSection) {
          tasksSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }, 60);
    }
  } else {
    stopActiveTransfersPolling();
  }

  if (window.location.hash !== `#${tabName}`) {
    window.location.hash = tabName;
  }
}

// -----------------------------------------------------------------------------
// Health Monitoring & Task Polling
// -----------------------------------------------------------------------------

export async function checkHealth() {
  try {
    const storageRes = await fetchStorageStatus().catch(() => null);
    if (storageRes) {
      const banner = document.getElementById('banner-storage');
      const bannerMsg = document.getElementById('banner-storage-msg');
      if (banner) {
        const nasOk = Boolean(storageRes.nas && storageRes.nas.mounted);
        const localOk = Boolean(storageRes.local && storageRes.local.mounted);

        if (nasOk && localOk) {
          banner.style.display = 'none';
        } else {
          banner.style.display = 'flex';
          if (bannerMsg) {
            if (!nasOk && !localOk) {
              bannerMsg.textContent = 'Both Local Staging and NAS Vault storage paths are offline or unconfigured.';
            } else if (!nasOk) {
              bannerMsg.textContent = `NAS Vault storage offline: ${storageRes.nas?.error || 'Path unconfigured or unmounted.'}`;
            } else {
              bannerMsg.textContent = `Local Staging storage offline: ${storageRes.local?.error || 'Path unconfigured or unwritable.'}`;
            }
          }
        }
      }
    }

    const dlRes = await fetchDownloadsStatus().catch(() => null);
    if (dlRes) {
      const circuitBanner = document.getElementById('banner-circuit');
      if (circuitBanner) {
        circuitBanner.style.display = dlRes.circuit_breaker_tripped ? 'flex' : 'none';
      }
    }

    // Refresh Active Stash Top-Nav Widget
    fetchActiveStash()
      .then(res => updateHeaderStashWidget(res.active_stash))
      .catch(() => {});

    const activeTask = await renderTasksView();

    // Stream telemetry directly to Reconcile view if active
    if (activeTask && activeTask.task_type === 'vault_reconcile') {
      window.updateReconcileFromHealth?.(activeTask);
    }

    const scrapeModal = document.getElementById('modal-scraper');
    if (scrapeModal && scrapeModal.style.display === 'flex' && activeTask && activeTask.task_type === 'library_scrape') {
      const statusEl = document.getElementById('scrape-modal-status');
      const summaryEl = document.getElementById('scrape-modal-summary');
      const progressEl = document.getElementById('scrape-modal-progress');
      const logEl = document.getElementById('scrape-modal-log');

      if (statusEl) {
        statusEl.textContent = (activeTask.status || 'RUNNING').toUpperCase();
        statusEl.className = 'status-badge status-queued';
      }
      if (summaryEl) summaryEl.textContent = activeTask.progress_label || 'Scraping library...';
      if (progressEl) progressEl.style.width = `${activeTask.progress_pct || 0}%`;
      if (logEl) {
        logEl.textContent = activeTask.log_output || '';
        logEl.scrollTop = logEl.scrollHeight;
      }
    }

    const currentlyRunning = Boolean(activeTask && activeTask.status === 'running');
    if (currentlyRunning !== isTaskRunning) {
      isTaskRunning = currentlyRunning;
      if (!isTaskRunning) {
        loadMetrics();
      }
    }
  } catch (err) {
    console.warn('Health check warning:', err);
  } finally {
    if (healthPollTimer) clearTimeout(healthPollTimer);
    healthPollTimer = setTimeout(checkHealth, isTaskRunning ? 2500 : 10000);
  }
}

// -----------------------------------------------------------------------------
// Modals & Storage Form Handlers
// -----------------------------------------------------------------------------

export function resetToHome(event) {
  if (event) event.preventDefault();

  const resetVal = id => { const el = document.getElementById(id); if (el) el.value = ''; };
  const resetChk = id => { const el = document.getElementById(id); if (el) el.checked = false; };

  resetVal('release-search');
  resetVal('release-creator-filter');
  resetVal('model-search');
  resetVal('pending-search');
  resetVal('model-creator-filter');
  resetVal('pending-creator-filter');
  resetChk('bundles-only');
  resetChk('bonus-filter');

  closeModal('release-modal');
  closeModal('files-modal');
  closeModal('config-modal');
  closeModal('browse-modal');
  closeModal('modal-scraper');
  closeModal('modal-ingest');
  closeModal('model-detail-modal');
  closeModal('modal-archive-viewer');

  switchTab('models');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

export function openModal(modalId) {
  if (modalId === 'modal-scraper') return openScraperModal();
  if (modalId === 'modal-ingest') return openIngestModal();
  if (modalId === 'config-modal') return openConfigModal();
  const el = document.getElementById(modalId);
  if (el) el.style.display = 'flex';
}

export function openConfigModal() {
  const rootEl = document.getElementById('setting-root-path');
  const workEl = document.getElementById('setting-work-path');
  const configPath = document.getElementById('config-path-input');
  const configWork = document.getElementById('config-work-input');

  if (configPath && rootEl) configPath.value = rootEl.value.trim();
  if (configWork && workEl) configWork.value = workEl.value.trim();

  const modal = document.getElementById('config-modal');
  if (modal) modal.style.display = 'flex';
}

export function closeModal(modalId) {
  const el = document.getElementById(modalId);
  if (el) el.style.display = 'none';
}

export async function saveStorageConfig() {
  const nasInput = document.getElementById('config-path-input');
  const workInput = document.getElementById('config-work-input');

  const nasPath = nasInput ? nasInput.value.trim() : '';
  const workPath = workInput ? workInput.value.trim() : '';

  if (!workPath) return alert('Please enter a valid Local Staging path.');
  if (!nasPath) return alert('Please enter a valid NAS / Vault Target path.');

  try {
    await apiSaveStorageConfig({ download_root_path: nasPath, download_work_path: workPath });
    closeModal('config-modal');
    checkHealth();
    refreshCurrentView();
  } catch (err) {
    alert(err.message);
  }
}

// -----------------------------------------------------------------------------
// Window Bindings for DOM Event Handlers
// -----------------------------------------------------------------------------

Object.assign(window, {

  // === 1. Core Navigation & Health Lifecycle ===
  checkHealth,
  loadMetrics,
  switchTab,
  refreshCurrentView,
  resetToHome,

  // === 2. Global Modal Management ===
  openModal,
  openConfigModal,
  closeModal,
  saveStorageConfig,

  // === 3. Releases & Monthly Continuity View ===
  loadReleases,
  loadAudit,
  openReleaseModal,
  queueReleasePack,
  cancelReleaseQueue,
  resetReleasePageAndLoad,
  changeReleasePage,

  // === 4. Models Catalog & Metadata Inspector ===
  loadModels,
  renderModelCard,
  openFilesModal,
  openModelDetailModal,
  resetModelPageAndLoad,
  changeModelPage,
  filterByTag,
  populateCreatorsFilter,
  openArchiveViewerModal,
  switchArchiveViewMode,
  triggerArchiveInspectionSweep,

  // === 5. Creators Directory ===
  loadCreators,
  filterByCreator,

  // === 6. Downloads: Pending & Queue Explorer ===
  switchDownloadSubView,
  resetPendingPageAndLoad,
  changePendingPage,
  loadPendingModels,
  toggleSelectPending,
  toggleSelectAllPending,
  queueSelectedModels,
  dequeueSelectedModels,
  queueSingleModel,
  dequeueSingleModel,
  clearQueueStatus,
  confirmPurgeIncomplete,

  // === 7. Downloads: Active Transfers & Queue Controls ===
  renderActiveTransfers,
  startActiveTransfersPolling,
  stopActiveTransfersPolling,
  controlDownload,

  // === 8. Downloads: History & Audit Log ===
  resetHistoryPageAndLoad,
  changeHistoryPage,
  loadDownloadHistory,

  // === 9. Downloads: Vault Forward & Deep Reconciliation ===
  setReconcileMode,
  triggerReconcileScan,
  cancelReconcile,
  loadReconcileView,
  updateReconcileFromHealth,
  resetSingleReconcileModel,
  resetAllReconcileDiscrepancies,

  // === 10. Downloads: Reverse Reconciliation (Orphans & Untracked Files) ===
  triggerOrphanAudit,
  deleteSingleOrphan,
  deleteAllOrphans,
  ingestSingleUntracked,
  ingestAllUntracked,

  // === 11. Downloads: Staging Scratch Pool Cleanup ===
  triggerStagingSweep,
  purgeSingleStagingGarbage,
  purgeAllStagingGarbage,

  // === 12. Downloads: Path Drift & Canonical Re-alignment ===
  triggerPathDriftScan,
  realignSinglePath,
  realignAllPaths,

  // === 13. Stashes ("A pile of gray") Workspace ===
  loadStashesView,
  updateHeaderStashWidget,
  selectStash,
  handleCreateStash,
  handleSetActiveStash,
  handleDeleteStash,
  handleRemoveStashItem,

  // === 14. Settings & Native MMF Authentication ===
  loadSettingsView,
  openDirectoryPicker,
  navigateBrowseFolder,
  selectCurrentBrowseFolder,
  saveSettingsForm,
  saveAuthSlug,
  saveAuthCredentials,
  toggleApiKeyVisibility,
  connectAccount,
  disconnectAccount,
  testAccountConnection,

  // === 15. Pipeline Tasks, Scraper & Ingest Automation ===
  renderTasksView,
  refreshTasksStatus: renderTasksView,
  openScraperModal,
  triggerLibraryScrape,
  openIngestModal,
  loadRecentScrapesForIngest,
  triggerMetadataIngest,
  renderIngestReport,
  cancelActiveTask,
  killActiveTask,
  clearTaskHistory,
  clearTaskLogView: () => {
    const log = document.getElementById('task-live-logs');
    if (log) log.textContent = '';
  },
  switchIngestTab: (tabId) => {
    const folderBtn = document.getElementById('tab-btn-folder');
    const manualBtn = document.getElementById('tab-btn-manual');
    const folderPane = document.getElementById('tab-scrape-folder');
    const manualPane = document.getElementById('tab-manual-ids');
    if (folderBtn && manualBtn) {
      folderBtn.classList.toggle('active', tabId === 'tab-scrape-folder');
      manualBtn.classList.toggle('active', tabId === 'tab-manual-ids');
    }
    if (folderPane) folderPane.style.display = (tabId === 'tab-scrape-folder') ? 'block' : 'none';
    if (manualPane) manualPane.style.display = (tabId === 'tab-manual-ids') ? 'block' : 'none';
  }
});

// -----------------------------------------------------------------------------
// Application Initialization
// -----------------------------------------------------------------------------

function initApp() {
  window.addEventListener('click', (event) => {
    if (event.target && event.target.classList && event.target.classList.contains('modal-backdrop')) {
      event.target.style.display = 'none';
    }
  });

  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      [
        'release-modal',
        'files-modal',
        'config-modal',
        'browse-modal',
        'modal-scraper',
        'modal-ingest',
        'model-detail-modal',
        'modal-archive-viewer'
      ].forEach(closeModal);
    }
  });

  // View Library dropdown click-to-toggle
  const dropdownEl = document.getElementById('nav-library-dropdown');
  const dropdownBtn = document.getElementById('btn-library-dropdown');
  if (dropdownEl && dropdownBtn) {
    dropdownBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      dropdownEl.classList.toggle('open');
    });
    document.addEventListener('click', () => {
      dropdownEl.classList.remove('open');
    });
  }

  const bindInput = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', fn);
  };

  bindInput('release-search', () => resetReleasePageAndLoad());
  bindInput('model-search', () => resetModelPageAndLoad());
  bindInput('pending-search', () => resetPendingPageAndLoad());

  window.addEventListener('hashchange', () => {
    const tab = window.location.hash.replace('#', '') || 'models';
    switchTab(tab);
  });

  const initialTab = window.location.hash.replace('#', '') || 'models';
  switchTab(initialTab);
  loadMetrics();
  checkHealth();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}
