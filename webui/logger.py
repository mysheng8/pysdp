"""
logger.py — AppLogger singleton for pysdp WebUI.

Features:
  - In-memory ring buffer (last MAX_MEMORY records) — fast API queries
  - Rotating file log  (5 MB × 3 files) in webui/logs/webui.log
  - Thread-safe
  - Four levels: debug / info / warning / error
  - PyLogLevel config (DEBUG/INFO/WARNING/ERROR) controls minimum level
  - Captures full traceback when an exception is passed

Usage:
    from logger import get_logger
    log = get_logger()

    log.info("Server started")
    log.debug("per-asset detail", context={"dc_id": 42})
    log.warning("SDPCLI unreachable", context={"url": "..."})
    try:
        ...
    except Exception as exc:
        log.error("Something broke", exc=exc, context={"path": "/api/..."})
"""

import logging
import logging.handlers
import sys
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LOG_DIR    = Path(__file__).parent / "logs"
_MAX_MEMORY = 300
_LEVEL_ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3}


def _read_py_log_level() -> str:
    """Read PyLogLevel from config. Returns lowercase level string."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from config import get_settings
        val = get_settings().get("PyLogLevel", "info").lower()
        if val in _LEVEL_ORDER:
            return val
    except Exception:
        pass
    return "info"


# ── Record ────────────────────────────────────────────────────────────────────

class _Record:
    __slots__ = ("id", "time", "level", "message", "traceback", "context")

    def __init__(
        self,
        id: int,
        level: str,
        message: str,
        tb: Optional[str] = None,
        context: Optional[dict] = None,
    ):
        self.id        = id
        self.time      = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.level     = level
        self.message   = message
        self.traceback = tb
        self.context   = context or {}

    def to_dict(self) -> dict:
        d: dict = {
            "id":      self.id,
            "time":    self.time,
            "level":   self.level,
            "message": self.message,
        }
        if self.traceback:
            d["traceback"] = self.traceback
        if self.context:
            d["context"] = self.context
        return d


# ── AppLogger ─────────────────────────────────────────────────────────────────

class AppLogger:
    def __init__(self, log_dir: Path = _LOG_DIR, max_memory: int = _MAX_MEMORY):
        self._records: deque = deque(maxlen=max_memory)
        self._counter = 0
        self._lock    = threading.Lock()
        self._min_level = _read_py_log_level()

        # Rotating file handler
        log_dir.mkdir(parents=True, exist_ok=True)
        flog = logging.getLogger("pysdp.webui")
        flog.setLevel(logging.DEBUG)
        flog.propagate = False
        if not flog.handlers:
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)-7s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
            h = logging.handlers.RotatingFileHandler(
                log_dir / "webui.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            h.setFormatter(fmt)
            flog.addHandler(h)

            # Also stream to stdout so the CMD window shows live logs
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            flog.addHandler(sh)
        self._flog = flog

    @property
    def is_debug(self) -> bool:
        return self._min_level == "debug"

    # ── Public API ────────────────────────────────────────────────────────────

    def debug(self, message: str, context: Optional[dict] = None) -> None:
        self._add("debug", message, context=context)

    def info(self, message: str, context: Optional[dict] = None) -> None:
        self._add("info", message, context=context)

    def warning(self, message: str, context: Optional[dict] = None) -> None:
        self._add("warning", message, context=context)

    def error(
        self,
        message: str,
        exc: Optional[BaseException] = None,
        context: Optional[dict] = None,
    ) -> None:
        self._add("error", message, exc=exc, context=context)

    def get_records(
        self,
        level: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Return records newest-first, optionally filtered by level."""
        with self._lock:
            recs = list(reversed(self._records))
        if level:
            recs = [r for r in recs if r.level == level]
        return [r.to_dict() for r in recs[offset: offset + limit]]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._records)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _add(
        self,
        level: str,
        message: str,
        exc: Optional[BaseException] = None,
        context: Optional[dict] = None,
    ) -> None:
        if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(self._min_level, 1):
            return

        tb = traceback.format_exc() if exc is not None else None
        # traceback.format_exc() returns "NoneType: None\n" when there's no
        # active exception — discard that noise.
        if tb and tb.strip() in ("NoneType: None", "None"):
            tb = None

        with self._lock:
            self._counter += 1
            rec = _Record(self._counter, level, message, tb, context)
            self._records.append(rec)

        # Mirror to file
        suffix = f" | {context}" if context else ""
        if level == "error":
            self._flog.error(message + suffix, exc_info=exc)
        elif level == "warning":
            self._flog.warning(message + suffix)
        elif level == "debug":
            self._flog.debug(message + suffix)
        else:
            self._flog.info(message + suffix)


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[AppLogger] = None
_init_lock = threading.Lock()


def get_logger() -> AppLogger:
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = AppLogger()
    return _instance
