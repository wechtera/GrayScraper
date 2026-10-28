/**
 * ui/static/js/utils.js - Shared UI formatting and sanitization utilities
 */

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

export function formatBytes(bytes, decimals = 1) {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

export function getFileExtension(filename) {
  if (!filename) return 'FILE';
  const parts = filename.split('.');
  return parts.length > 1 ? parts.pop().toUpperCase() : 'FILE';
}

export function renderStatusBadge(status) {
  const s = (status || 'not_downloaded').toLowerCase();
  switch (s) {
    case 'completed':
      return '<span class="status-badge status-completed">Archived</span>';
    case 'downloading':
    case 'staging_complete':
    case 'offloading':
      return `<span class="status-badge status-queued">${s.replace('_', ' ')}</span>`;
    case 'queued':
      return '<span class="status-badge status-queued">Queued</span>';
    case 'partial':
      return '<span class="status-badge status-partial">Partial</span>';
    case 'failed':
      return '<span class="status-badge status-failed">Failed</span>';
    default:
      return '<span class="status-badge status-not_downloaded">Not Downloaded</span>';
  }
}
