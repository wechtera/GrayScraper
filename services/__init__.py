"""
services - Core engine, domain models, storage reconciliation, and background pipeline orchestrators.
"""

from .analytics import calculate_month_sequence, compute_catalog_audit, run_startup_analytics
from .archive_peeker import ArchivePeeker
from .auth_service import fetch_user_profile, get_auth_status, get_setting, set_settings
from .db import get_db_connection, init_persistent_tables
from .downloader import DownloadManager
from .reconciler import VaultReconciler
from .stash_service import StashService
from .task_manager import TaskManager

__all__ = [
    "get_db_connection",
    "init_persistent_tables",
    "calculate_month_sequence",
    "run_startup_analytics",
    "compute_catalog_audit",
    "ArchivePeeker",
    "VaultReconciler",
    "DownloadManager",
    "TaskManager",
    "StashService",
    "get_setting",
    "set_settings",
    "fetch_user_profile",
    "get_auth_status",
    "StorageUnavailableError",
    "CircuitBreakerTrippedError",
    "StorageManager",
    "DownloadManager",
    "PathSanitizer",
    "FileValidator",
    "DiskAdopter",
]
