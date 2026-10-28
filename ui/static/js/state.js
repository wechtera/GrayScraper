/**
 * Centralized mutable client state store.
 */
export const state = {
  PAGE_SIZE: 50,
  pendingCurrentPage: 1,
  pendingTotalCount: 0,
  selectedPendingModelIds: new Set(),
  historyCurrentPage: 1,
  historyTotalCount: 0,
  pollInterval: null
};
