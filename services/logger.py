"""
services/logger.py - Centralized rotating file logger with dynamic subsystem layer filtering.
"""

from collections import deque
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Set

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

APP_LOG_PATH = LOGS_DIR / "vault.log"

# Supported diagnostic subsystem layers
VALID_LAYERS = {"http", "storage", "archive", "sql"}

# In-memory circular buffer for live UI log streaming (max 1,000 lines)
_UI_LOG_BUFFER = deque(maxlen=1000)
_BUFFER_LOCK = threading.Lock()
_CONFIG_LOCK = threading.Lock()

# Dynamic runtime configuration (defaults)
_ACTIVE_LEVEL = logging.INFO
_ACTIVE_LAYERS: Set[str] = set()


class UILogHandler(logging.Handler):
    """Pushes formatted records to an in-memory circular buffer for the web UI."""
    def emit(self, record):
        try:
            msg = self.format(record)
            with _BUFFER_LOCK:
                _UI_LOG_BUFFER.append(msg)
        except Exception:
            self.handleError(record)


def get_ui_logs(tail: int = 200) -> List[str]:
    """Returns the latest log lines from the in-memory ring buffer."""
    with _BUFFER_LOCK:
        if tail >= len(_UI_LOG_BUFFER):
            return list(_UI_LOG_BUFFER)
        return list(_UI_LOG_BUFFER)[-tail:]


def clear_ui_logs():
    """Clears the live console buffer."""
    with _BUFFER_LOCK:
        _UI_LOG_BUFFER.clear()


class LayerFilter(logging.Filter):
    """Filters log records based on the active log level and subsystem layer."""
    def filter(self, record: logging.LogRecord) -> bool:
        layer = getattr(record, "layer", None)
        # If the record belongs to a specific layer and that layer is disabled, suppress it
        if layer and layer in VALID_LAYERS:
            with _CONFIG_LOCK:
                if layer not in _ACTIVE_LAYERS:
                    return False
        return True


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("vault")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)  # Root level stays DEBUG; handlers filter
    logger.addFilter(LayerFilter())

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Rotating File Handler (10 MB * 4 files = 40 MB max disk usage)
    file_handler = RotatingFileHandler(
        APP_LOG_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # 2. In-Memory UI Handler
    ui_handler = UILogHandler()
    ui_handler.setFormatter(formatter)
    ui_handler.setLevel(logging.DEBUG)
    logger.addHandler(ui_handler)

    # 3. Standard Console stdout
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


# -----------------------------------------------------------------------------
# Dynamic Configuration Helpers
# -----------------------------------------------------------------------------

def set_logging_config(level_name: str, enabled_layers: List[str]):
    """Dynamically updates active log levels and enabled subsystem layers."""
    global _ACTIVE_LEVEL, _ACTIVE_LAYERS

    lvl = getattr(logging, level_name.upper(), logging.INFO)
    clean_layers = {l.strip().lower() for l in enabled_layers if l.strip().lower() in VALID_LAYERS}

    with _CONFIG_LOCK:
        _ACTIVE_LEVEL = lvl
        _ACTIVE_LAYERS = clean_layers
        for handler in logger.handlers:
            handler.setLevel(_ACTIVE_LEVEL)

    logger.info(f"[Config] Updated logging: Level={logging.getLevelName(lvl)}, Active Layers={sorted(clean_layers)}")


def get_logging_config() -> Dict[str, Any]:
    """Returns the current logging configuration."""
    with _CONFIG_LOCK:
        return {
            "level": logging.getLevelName(_ACTIVE_LEVEL),
            "layers": sorted(list(_ACTIVE_LAYERS)),
            "available_layers": sorted(list(VALID_LAYERS)),
        }


def init_logging_from_db(db_path: Path):
    """Initializes logging settings from the SQLite database."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT key, value FROM app_settings WHERE key IN ('log_level', 'log_layers');").fetchall()
            settings = {r["key"]: r["value"] for r in rows}

            level = settings.get("log_level", "INFO")
            layers_raw = settings.get("log_layers", "")
            layers = [l.strip() for l in layers_raw.split(",") if l.strip()]

            set_logging_config(level, layers)
    except Exception as e:
        logger.warning(f"Could not load logging configuration from database: {e}")


# -----------------------------------------------------------------------------
# Subsystem Layer Logging Dispatcher
# -----------------------------------------------------------------------------

def log_layer(layer: str, level: int, message: str, **kwargs):
    """
    Emits a log entry tagged with a specific subsystem layer.
    Aborts immediately if the subsystem layer is not enabled.
    """
    clean_layer = layer.strip().lower()
    with _CONFIG_LOCK:
        if clean_layer in VALID_LAYERS and clean_layer not in _ACTIVE_LAYERS:
            return

    extra = kwargs.get("extra", {})
    extra["layer"] = clean_layer
    kwargs["extra"] = extra

    tag = f"[{clean_layer.upper()}] "
    logger.log(level, f"{tag}{message}", **kwargs)
