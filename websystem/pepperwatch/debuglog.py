"""A debug log that Settings starts, stops and offers for download.

While one runs, the web process and the worker each write their log to
``<root>/<id>/web.log`` and ``worker.log``, with Pepper Watch's own DEBUG lines
included (the journal keeps INFO and up, as before). The marker file
``<root>/active`` holds the running log's id. Each process checks it, the worker
once a second and the web on every debug-log request and at start, so a process
that restarts, often the very fault being chased, carries on writing.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import platform
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Per process and log: the file and two rotated copies, 30 MB at most.
MAX_BYTES = 10_000_000
BACKUPS = 2
KEEP = 5  # logs kept; starting a sixth deletes the oldest
_ID = re.compile(r"^\d{8}T\d{6}Z$")

_lock = threading.Lock()
_handler: logging.Handler | None = None
_writing: str | None = None


def active(root: Path) -> str | None:
    """The running debug log's id, or None."""
    try:
        value = (root / "active").read_text().strip()
    except OSError:
        return None
    return value if _ID.match(value) else None


def start(root: Path) -> str:
    """Start a debug log (or return the one already running)."""
    running = active(root)
    if running:
        return running
    log_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (root / log_id).mkdir(parents=True, exist_ok=True)
    for old in _folders(root)[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    marker = root / "active.tmp"
    marker.write_text(log_id)
    os.replace(marker, root / "active")
    return log_id


def stop(root: Path) -> str | None:
    """End the running debug log; returns its id."""
    running = active(root)
    (root / "active").unlink(missing_ok=True)
    return running


def sync(root: Path, process: str) -> None:
    """Write to the running debug log if there is one, and stop writing once it has ended."""
    global _handler, _writing
    wanted = active(root)
    with _lock:
        if wanted == _writing:
            return
        own = logging.getLogger("pepperwatch")
        if _handler is not None:
            log.info("debug log %s ends here", _writing)
            logging.getLogger().removeHandler(_handler)
            _handler.close()
            own.setLevel(logging.NOTSET)
            _handler = _writing = None
        if wanted is None:
            return
        folder = root / wanted
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            folder / f"{process}.log", maxBytes=MAX_BYTES, backupCount=BACKUPS)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"))
        top = logging.getLogger()
        # The journal stays at INFO: the DEBUG lines go to this file only.
        for existing in top.handlers:
            if existing.level == logging.NOTSET:
                existing.setLevel(logging.INFO)
        top.addHandler(handler)
        own.setLevel(logging.DEBUG)
        _handler, _writing = handler, wanted
    import numpy

    log.info("debug log %s: %s, pid %d, Python %s, NumPy %s, %s",
             wanted, process, os.getpid(), platform.python_version(), numpy.__version__, platform.platform())


def logs(root: Path) -> list[dict[str, Any]]:
    """Every kept debug log, newest first."""
    running = active(root)
    return [
        {
            "id": folder.name,
            "started_at": datetime.strptime(folder.name, "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=timezone.utc).isoformat(timespec="seconds"),
            "bytes": sum(f.stat().st_size for f in folder.iterdir() if f.is_file()),
            "active": folder.name == running,
        }
        for folder in reversed(_folders(root))
    ]


def archive(root: Path, log_id: str) -> bytes | None:
    """One debug log's files as a zip, or None if there is no such log."""
    folder = root / log_id
    if not _ID.match(log_id) or not folder.is_dir():
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for file in sorted(folder.iterdir()):
            if file.is_file():
                bundle.write(file, f"pepperwatch-debug-{log_id}/{file.name}")
    return buffer.getvalue()


def _folders(root: Path) -> list[Path]:
    """Debug log folders, oldest first (their names are UTC timestamps)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and _ID.match(p.name))
