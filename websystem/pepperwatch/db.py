"""SQLite access layer.

Manuscript §3.3.2.1 calls for WAL mode so the dashboard can read history while
the worker keeps logging detections. Three implementation details make that
actually hold under load, and all three are load-bearing:

* **One connection per thread.** gunicorn's gthread workers and the worker's
  capture/inference threads all touch the database; a shared handle would need
  a global lock and would serialise exactly what WAL exists to parallelise.
* **``BEGIN IMMEDIATE`` for every write.** Without it, two connections can each
  open a deferred read transaction and then try to upgrade, which SQLite can
  only resolve by failing one with ``SQLITE_BUSY`` — a deadlock that no
  ``busy_timeout`` can rescue. Taking the write lock up front turns that into a
  brief, retryable wait.
* **Short transactions, never spanning a stream.** A read transaction held open
  across an SSE or MJPEG response would block WAL checkpointing for as long as
  the phone stayed connected, and the ``-wal`` file would grow without bound.
  Every read here opens and closes inside a single call.

On §3.5's wording: WAL *does* raise ``SQLITE_BUSY`` under contention — that is
normal, not a defect. What matters, and what ``tests/test_db_concurrency.py``
asserts, is that no lock error escapes unhandled and no reader waits longer
than the configured ``busy_timeout``.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import Config
from .models import Detection, union_area

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = "1"

# A write that loses the lock race is retried with jittered backoff. Five
# attempts over ~1.5 s comfortably outlasts any transaction this system takes,
# and the jitter stops the worker and the web process from resonating.
_WRITE_ATTEMPTS = 5
_WRITE_BACKOFF_S = 0.05


def utcnow() -> str:
    """Current time as ISO-8601 UTC, the format every timestamp column uses."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    """Thread-safe SQLite access with WAL tuning applied per connection."""

    def __init__(self, cfg: Config) -> None:
        self._path = cfg.db_path
        self._busy_timeout_ms = int(cfg.db.busy_timeout_ms)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialised = False

    @property
    def path(self) -> Path:
        return self._path

    # -- connections -------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """The calling thread's connection, opened and tuned on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_ms / 1000.0,
            # Transactions are managed explicitly via BEGIN IMMEDIATE rather
            # than by the driver's implicit behaviour.
            isolation_level=None,
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        self._tune(conn)
        self._local.conn = conn
        return conn

    def _tune(self, conn: sqlite3.Connection) -> None:
        # busy_timeout on EVERY connection, read-only ones included: a reader
        # that refuses to wait is just as fatal to a page load as a writer.
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL can lose the last few transactions on an abrupt power cut but
        # never corrupts the file. For a solar-powered device that loses power
        # nightly by design, that is the right trade against an fsync per commit
        # on an SD card. Stated here because §3.3.2.1 leaves it implicit.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        # 8 MB page cache. Enough for the gallery and weekly-stats queries,
        # small enough to be irrelevant against the Pi 5's 8 GB.
        conn.execute("PRAGMA cache_size = -8000")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")

    def close(self) -> None:
        """Close this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A short read. Must not wrap a streaming response — see module docs."""
        yield self.connection()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction, retried with backoff if the lock is contended."""
        conn = self.connection()
        last: sqlite3.OperationalError | None = None

        for attempt in range(_WRITE_ATTEMPTS):
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                last = exc
                time.sleep(_WRITE_BACKOFF_S * (2**attempt) * (0.5 + random.random()))
                continue

            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return

        raise sqlite3.OperationalError(
            f"could not acquire the write lock after {_WRITE_ATTEMPTS} attempts: {last}"
        )

    # -- schema ------------------------------------------------------------

    def initialise(self, *, writer: str = "worker") -> None:
        """Apply the schema and record which process is the designated writer.

        ``writer`` resolves the manuscript's ambiguity about who logs detections
        (§3.3 says the inference layer, §3.4.3 says the backend). Both are
        supported; recording the choice means a misconfiguration that would
        produce duplicate rows is caught at startup.
        """
        with self._init_lock:
            if self._initialised:
                return
            conn = self.connection()
            conn.executescript(SCHEMA_PATH.read_text())
            now = utcnow()
            conn.executemany(
                "INSERT INTO schema_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                [("schema_version", SCHEMA_VERSION, now), ("writer", writer, now)],
            )
            self._initialised = True
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            log.info("database ready at %s (journal_mode=%s, writer=%s)", self._path, mode, writer)

    def meta(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -- writes ------------------------------------------------------------

    def insert_event(
        self,
        *,
        site: str,
        frame_seq: int,
        detections: Sequence[Detection],
        model_version: str,
        conf_threshold: float,
        iou_threshold: float,
        severity: str,
        trigger: str = "auto",
        captured_at: str | None = None,
        image_path: str | None = None,
        thumb_path: str | None = None,
        inference_ms: float | None = None,
        preview_fps: float | None = None,
        inference_fps: float | None = None,
        synthetic: bool = False,
        note: str | None = None,
    ) -> int:
        """Write one detection event and its boxes in a single transaction.

        The denormalized ``detection_count``, ``max_confidence`` and
        ``damaged_area`` are computed here rather than by callers, so they can
        never drift from the rows they summarise.

        Idempotent on ``(site, frame_seq, captured_at)``: a retried publish
        returns the existing id instead of duplicating the event.
        """
        captured_at = captured_at or utcnow()
        damaged = [d for d in detections if d.class_name != "healthy"]
        max_conf = max((d.confidence for d in detections), default=None)

        with self.write() as conn:
            existing = conn.execute(
                "SELECT id FROM detection_event WHERE site = ? AND frame_seq = ? AND captured_at = ?",
                (site, frame_seq, captured_at),
            ).fetchone()
            if existing:
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO detection_event (
                    captured_at, site, image_path, thumb_path, frame_seq,
                    model_version, conf_threshold, iou_threshold,
                    inference_ms, preview_fps, inference_fps,
                    detection_count, max_confidence, damaged_area,
                    severity, trigger, synthetic, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at, site, image_path, thumb_path, frame_seq,
                    model_version, conf_threshold, iou_threshold,
                    inference_ms, preview_fps, inference_fps,
                    len(detections), max_conf, union_area(damaged),
                    severity, trigger, int(synthetic), note,
                ),
            )
            event_id = int(cursor.lastrowid or 0)

            if detections:
                conn.executemany(
                    """
                    INSERT INTO bounding_box
                        (event_id, class_id, class_name, confidence, x, y, w, h)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            event_id, d.class_id, d.class_name, d.confidence,
                            d.x, d.y, d.w, d.h,
                        )
                        for d in (det.clipped() for det in detections)
                    ],
                )
            return event_id

    def log_system(
        self,
        *,
        event: str,
        level: str = "info",
        source: str = "worker",
        telemetry: dict[str, Any] | None = None,
        detail: str | None = None,
        logged_at: str | None = None,
    ) -> int:
        """Append a hardware-health or diagnostic row."""
        t = telemetry or {}
        with self.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO system_log (
                    logged_at, level, source, event,
                    cpu_temp_c, cpu_percent, mem_used_mb, mem_total_mb, disk_free_mb,
                    voltage_v, current_ma, power_w, battery_pct, throttled,
                    synthetic, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    logged_at or utcnow(), level, source, event,
                    t.get("cpu_temp_c"), t.get("cpu_percent"), t.get("mem_used_mb"),
                    t.get("mem_total_mb"), t.get("disk_free_mb"),
                    t.get("voltage_v"), t.get("current_ma"), t.get("power_w"),
                    t.get("battery_pct"), int(bool(t.get("throttled"))),
                    int(bool(t.get("synthetic"))), detail,
                ),
            )
            return int(cursor.lastrowid or 0)

    def set_archived(self, event_id: int, archived: bool) -> bool:
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE detection_event SET archived = ? WHERE id = ?",
                (int(archived), event_id),
            )
            return cursor.rowcount > 0

    # -- reads -------------------------------------------------------------

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        """One event with its boxes, or ``None``."""
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM detection_event WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            boxes = conn.execute(
                "SELECT class_id, class_name, confidence, x, y, w, h "
                "FROM bounding_box WHERE event_id = ? ORDER BY confidence DESC",
                (event_id,),
            ).fetchall()
        event = dict(row)
        event["boxes"] = [dict(b) for b in boxes]
        return event

    def list_events(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        class_name: str | None = None,
        severity: str | None = None,
        min_confidence: float | None = None,
        archived: bool | None = False,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filtered, paginated event list for the Historical Detection Log.

        Returns ``{"total": int, "items": [...], "limit": int, "offset": int}``.
        The total is a separate COUNT over the same predicate so the UI can show
        "showing 1-50 of 412" without fetching everything.
        """
        where: list[str] = []
        params: list[Any] = []

        if since:
            where.append("captured_at >= ?")
            params.append(since)
        if until:
            where.append("captured_at <= ?")
            params.append(until)
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if min_confidence is not None:
            where.append("max_confidence >= ?")
            params.append(min_confidence)
        if archived is not None:
            where.append("archived = ?")
            params.append(int(archived))
        if query:
            where.append("(note LIKE ? OR site LIKE ? OR severity LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        if class_name:
            where.append(
                "EXISTS (SELECT 1 FROM bounding_box b "
                "WHERE b.event_id = detection_event.id AND b.class_name = ?)"
            )
            params.append(class_name)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        with self.read() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM detection_event {clause}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM detection_event {clause} "
                "ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

        return {
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "items": [dict(r) for r in rows],
        }

    def weekly_stats(self, *, days: int = 7, site: str | None = None) -> dict[str, Any]:
        """Everything the KPI tiles need.

        Compares the trailing ``days`` window against the window before it so
        the tiles can show a delta and a trend, which is what Figure 3.4 draws.
        """
        now = datetime.now(timezone.utc)
        current_from = (now - timedelta(days=days)).isoformat(timespec="milliseconds")
        previous_from = (now - timedelta(days=2 * days)).isoformat(timespec="milliseconds")

        site_clause = " AND site = ?" if site else ""
        site_params: list[Any] = [site] if site else []

        with self.read() as conn:
            current = conn.execute(
                f"""
                SELECT COUNT(*) AS events,
                       COALESCE(SUM(detection_count), 0) AS detections,
                       AVG(max_confidence) AS avg_confidence,
                       AVG(damaged_area) AS avg_area
                FROM detection_event
                WHERE captured_at >= ?{site_clause}
                """,
                [current_from, *site_params],
            ).fetchone()
            previous = conn.execute(
                f"""
                SELECT COUNT(*) AS events,
                       COALESCE(SUM(detection_count), 0) AS detections
                FROM detection_event
                WHERE captured_at >= ? AND captured_at < ?{site_clause}
                """,
                [previous_from, current_from, *site_params],
            ).fetchone()
            damaged_before = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM bounding_box b
                JOIN detection_event e ON e.id = b.event_id
                WHERE b.class_name = 'aphid_damage'
                  AND e.captured_at >= ? AND e.captured_at < ?{(' AND e.site = ?' if site else '')}
                """,
                [previous_from, current_from, *site_params],
            ).fetchone()["n"]
            distribution = conn.execute(
                f"""
                SELECT b.class_name AS class_name, COUNT(*) AS n
                FROM bounding_box b
                JOIN detection_event e ON e.id = b.event_id
                WHERE e.captured_at >= ?{(' AND e.site = ?' if site else '')}
                GROUP BY b.class_name
                """,
                [current_from, *site_params],
            ).fetchall()
            worst = conn.execute(
                f"""
                SELECT severity, COUNT(*) AS n
                FROM detection_event
                WHERE captured_at >= ?{site_clause}
                GROUP BY severity
                """,
                [current_from, *site_params],
            ).fetchall()

        detections_now = int(current["detections"] or 0)
        detections_before = int(previous["detections"] or 0)
        distribution_now = {row["class_name"]: int(row["n"]) for row in distribution}

        # The delta and trend follow aphid damage, not every box: more healthy
        # leaves scanned is not bad news. With no damage the week before there is
        # no percentage to give, so delta_pct is None.
        damaged_now = distribution_now.get("aphid_damage", 0)
        damaged_before = int(damaged_before or 0)
        if damaged_before:
            delta_pct = 100.0 * (damaged_now - damaged_before) / damaged_before
        else:
            delta_pct = None if damaged_now else 0.0

        # A +/-10% band reads as "Stable" rather than flapping between Rising and
        # Falling on noise, which is what an agricultural officer wants from a
        # weekly trend indicator.
        if delta_pct is None:
            trend = "rising"
        elif abs(delta_pct) < 10.0:
            trend = "stable"
        elif delta_pct > 0:
            trend = "rising"
        else:
            trend = "falling"

        severity_counts = {row["severity"]: int(row["n"]) for row in worst}
        from .models import SEVERITY_ORDER

        overall = "none"
        for band in reversed(SEVERITY_ORDER):
            if severity_counts.get(band):
                overall = band
                break

        return {
            "days": days,
            "site": site,
            "from": current_from,
            "events": int(current["events"] or 0),
            "events_previous": int(previous["events"] or 0),
            "detections": detections_now,
            "detections_previous": detections_before,
            "damaged": damaged_now,
            "damaged_previous": damaged_before,
            "delta_pct": None if delta_pct is None else round(delta_pct, 1),
            "avg_confidence": round(current["avg_confidence"], 4) if current["avg_confidence"] else 0.0,
            "avg_damaged_area": round(current["avg_area"], 5) if current["avg_area"] else 0.0,
            "trend": trend,
            "severity": overall,
            "severity_counts": severity_counts,
            "distribution": distribution_now,
        }

    def latest_telemetry(self) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM system_log WHERE event = 'telemetry' "
                "ORDER BY logged_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def iter_export_rows(
        self, *, since: str | None = None, until: str | None = None
    ) -> Iterator[sqlite3.Row]:
        """Stream the joined event+box rows for CSV export.

        Yields row by row rather than materialising the result, so exporting
        months of history does not spike memory on the Pi. Left-joined so an
        event with no detections still appears — a clean scan is data too.
        """
        where: list[str] = []
        params: list[Any] = []
        if since:
            where.append("e.captured_at >= ?")
            params.append(since)
        if until:
            where.append("e.captured_at <= ?")
            params.append(until)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        with self.read() as conn:
            cursor = conn.execute(
                f"""
                SELECT e.id AS event_id, e.captured_at, e.site, e.severity,
                       e.detection_count, e.max_confidence, e.damaged_area,
                       e.model_version, e.conf_threshold, e.iou_threshold,
                       e.inference_ms, e.preview_fps, e.inference_fps,
                       e.trigger, e.synthetic, e.image_path,
                       b.class_name, b.confidence, b.x, b.y, b.w, b.h
                FROM detection_event e
                LEFT JOIN bounding_box b ON b.event_id = e.id
                {clause}
                ORDER BY e.captured_at DESC, e.id DESC, b.confidence DESC
                """,
                params,
            )
            while True:
                batch = cursor.fetchmany(256)
                if not batch:
                    return
                yield from batch

    # -- maintenance -------------------------------------------------------

    def clear_detections(self) -> int:
        """Delete every detection event and its boxes. Returns the events removed.

        Only on an operator's explicit request (Settings > Clear all
        detections). Telemetry in system_log is kept: it is diagnostics, not
        detections.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM bounding_box")
            removed = conn.execute("DELETE FROM detection_event").rowcount
        log.warning("cleared %d detection events on request", removed)
        return removed

    def prune(self, *, retention_days: int) -> int:
        """Delete telemetry older than the retention window.

        Only ``system_log`` is pruned. Detection events are the research record
        and are never deleted automatically; telemetry at one sample every 15 s
        is what would otherwise grow without bound and wear the SD card.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="milliseconds"
        )
        with self.write() as conn:
            cursor = conn.execute(
                "DELETE FROM system_log WHERE logged_at < ? AND event = 'telemetry'",
                (cutoff,),
            )
            removed = cursor.rowcount
        if removed:
            log.info("pruned %d telemetry rows older than %s", removed, cutoff)
        return removed

    def checkpoint(self) -> dict[str, int]:
        """Truncate the WAL.

        ``wal_autocheckpoint`` only fires on a *writing* connection, so an idle
        overnight worker leaves the ``-wal`` file at whatever size the day's
        logging grew it to. The maintenance timer calls this to actually bound
        it — see the note in the deploy units.
        """
        conn = self.connection()
        busy, log_pages, checkpointed = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        return {"busy": int(busy), "log_pages": int(log_pages), "checkpointed": int(checkpointed)}

    def stats(self) -> dict[str, Any]:
        """Row counts and on-disk footprint, for /api/health and `pw doctor`."""
        with self.read() as conn:
            counts = {
                name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                for name in ("detection_event", "bounding_box", "system_log")
            }
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        wal = self._path.with_name(self._path.name + "-wal")
        return {
            "path": str(self._path),
            "journal_mode": journal_mode,
            "size_mb": round(self._path.stat().st_size / 1e6, 3) if self._path.exists() else 0.0,
            "wal_mb": round(wal.stat().st_size / 1e6, 3) if wal.exists() else 0.0,
            "counts": counts,
        }
