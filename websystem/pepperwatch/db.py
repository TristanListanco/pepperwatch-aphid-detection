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
        # FULL syncs the WAL on every commit, so a detection that was committed
        # survives a power cut; NORMAL could lose the last ~30 s of them. The
        # worker commits a few times a minute (events, 15 s telemetry), so the
        # extra SD-card writes are negligible. §3.3.2.1 leaves this implicit.
        conn.execute("PRAGMA synchronous = FULL")
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
            _add_missing_columns(conn)
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

    def set_meta(self, key: str, value: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO schema_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, utcnow()),
            )

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
        camera: int = 0,
        capture_id: str | None = None,
        capture_lead: bool = True,
        camera_name: str | None = None,
    ) -> int:
        """Write one detection event and its boxes in a single transaction.

        The denormalized ``detection_count``, ``max_confidence`` and
        ``damaged_area`` are computed here rather than by callers, so they can
        never drift from the rows they summarise.

        Idempotent on ``(site, camera, frame_seq, captured_at)``: a retried
        publish returns the existing id instead of duplicating the event.
        """
        captured_at = captured_at or utcnow()
        damaged = [d for d in detections if d.class_name != "healthy"]
        max_conf = max((d.confidence for d in detections), default=None)

        with self.write() as conn:
            existing = conn.execute(
                "SELECT id FROM detection_event "
                "WHERE site = ? AND camera = ? AND frame_seq = ? AND captured_at = ?",
                (site, camera, frame_seq, captured_at),
            ).fetchone()
            if existing:
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO detection_event (
                    captured_at, site, camera, image_path, thumb_path, frame_seq,
                    model_version, conf_threshold, iou_threshold,
                    inference_ms, preview_fps, inference_fps,
                    detection_count, max_confidence, damaged_area,
                    severity, trigger, synthetic, note,
                    capture_id, capture_lead, camera_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at, site, camera, image_path, thumb_path, frame_seq,
                    model_version, conf_threshold, iou_threshold,
                    inference_ms, preview_fps, inference_fps,
                    len(detections), max_conf, union_area(damaged),
                    severity, trigger, int(synthetic), note,
                    capture_id, int(capture_lead), camera_name,
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
        """Archive a scan, and the other cameras' rows of its photo with it."""
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE detection_event SET archived = ? WHERE id = ? OR capture_id = "
                "(SELECT capture_id FROM detection_event WHERE id = ?)",
                (int(archived), event_id, event_id),
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
            cameras = _capture_cameras(conn, [row["capture_id"]]).get(row["capture_id"], [])
        event = dict(row)
        event["boxes"] = [dict(b) for b in boxes]
        event["cameras"] = cameras
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

        One item per photo: of a multi-camera capture's rows only the lead is
        listed, with every camera's result in ``cameras``. A filter on what was
        found matches when any camera in the photo matches it.
        """
        where: list[str] = ["capture_lead = 1"]
        params: list[Any] = []
        found: list[str] = []  # tested against every camera's row of the photo
        found_params: list[Any] = []

        if since:
            where.append("captured_at >= ?")
            params.append(since)
        if until:
            where.append("captured_at <= ?")
            params.append(_through(until))
        if severity:
            found.append("x.severity = ?")
            found_params.append(severity)
        if min_confidence is not None:
            found.append("x.max_confidence >= ?")
            found_params.append(min_confidence)
        if archived is not None:
            where.append("archived = ?")  # a photo's rows are archived together
            params.append(int(archived))
        if query:
            found.append("(x.note LIKE ? OR x.site LIKE ? OR x.severity LIKE ? OR x.camera_name LIKE ?)")
            found_params.extend([f"%{query}%"] * 4)
        if class_name:
            found.append("EXISTS (SELECT 1 FROM bounding_box b WHERE b.event_id = x.id AND b.class_name = ?)")
            found_params.append(class_name)
        if found:
            where.append(
                "EXISTS (SELECT 1 FROM detection_event x WHERE (x.id = detection_event.id OR "
                f"x.capture_id = detection_event.capture_id) AND {' AND '.join(found)})"
            )
            params.extend(found_params)

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
            cameras = _capture_cameras(conn, [r["capture_id"] for r in rows])

        items = [dict(r) for r in rows]
        for item in items:
            item["cameras"] = cameras.get(item["capture_id"], [])
        return {
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    def weekly_stats(self, *, days: int = 7, site: str | None = None) -> dict[str, Any]:
        """Everything the KPI tiles need.

        Compares the trailing ``days`` window against the window before it so
        the tiles can show a delta and a trend, which is what Figure 3.4 draws.
        Synthetic events (the stub detector's, with no model installed) are left
        out: they are not measurements.
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
                WHERE captured_at >= ? AND synthetic = 0{site_clause}
                """,
                [current_from, *site_params],
            ).fetchone()
            previous = conn.execute(
                f"""
                SELECT COUNT(*) AS events,
                       COALESCE(SUM(detection_count), 0) AS detections
                FROM detection_event
                WHERE captured_at >= ? AND captured_at < ? AND synthetic = 0{site_clause}
                """,
                [previous_from, current_from, *site_params],
            ).fetchone()
            # CROSS JOIN keeps the events outer: only the weeks' boxes are read.
            # As a plain JOIN SQLite starts from idx_box_class instead, every
            # aphid box of the season (~470 ms at 1.7M boxes, every 30 s per phone).
            damaged_before = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM detection_event e
                CROSS JOIN bounding_box b ON b.event_id = e.id
                WHERE b.class_name = 'aphid_damage' AND e.synthetic = 0
                  AND e.captured_at >= ? AND e.captured_at < ?{(' AND e.site = ?' if site else '')}
                """,
                [previous_from, current_from, *site_params],
            ).fetchone()["n"]
            distribution = conn.execute(
                f"""
                SELECT b.class_name AS class_name, COUNT(*) AS n
                FROM detection_event e
                CROSS JOIN bounding_box b ON b.event_id = e.id
                WHERE e.captured_at >= ? AND e.synthetic = 0{(' AND e.site = ?' if site else '')}
                GROUP BY b.class_name
                """,
                [current_from, *site_params],
            ).fetchall()
            worst = conn.execute(
                f"""
                SELECT severity, COUNT(*) AS n
                FROM detection_event
                WHERE captured_at >= ? AND synthetic = 0{site_clause}
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

    def recent_status(self, *, hours: int = 24, fallback_days: int = 7, site: str | None = None) -> dict[str, Any]:
        """The farmer's answer to "how are my plants": what recent photos show.

        For each camera, the median severity of its real photos over the last
        ``hours`` (ties go to the more severe band), so one false alarm cannot
        set it; then the camera whose plants look worst, so damage in front of
        one camera is not averaged away by a clean one. With no photos in that
        window, the last ``fallback_days``.

        Only photos with leaves count: one where the detector found nothing
        says nothing about the plants. With none in either window,
        ``no_leaves`` says how many photos the camera did take, so the
        dashboard can tell "aim the camera" from "no photos yet".
        """
        from .models import SEVERITY_ORDER

        now = datetime.now(timezone.utc)
        site_clause = " AND site = ?" if site else ""
        windows = (("24h", now - timedelta(hours=hours)), ("7d", now - timedelta(days=fallback_days)))
        # Photos, not rows: one photo of several cameras has a row for each.
        photos_since = (
            "SELECT COUNT(DISTINCT COALESCE(capture_id, id)) FROM detection_event "
            f"WHERE captured_at >= ? AND synthetic = 0{site_clause}"
        )
        for basis, since in windows:
            with self.read() as conn:
                photos = conn.execute(
                    f"{photos_since} AND detection_count > 0",
                    [since.isoformat(timespec="milliseconds"), *([site] if site else [])],
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT camera, severity, COUNT(*) AS n
                    FROM detection_event
                    WHERE captured_at >= ? AND synthetic = 0 AND detection_count > 0{site_clause}
                    GROUP BY camera, severity
                    """,
                    [since.isoformat(timespec="milliseconds"), *([site] if site else [])],
                ).fetchall()
            if not rows:
                continue
            cameras: dict[int, dict[str, int]] = {}
            for row in rows:
                cameras.setdefault(int(row["camera"] or 0), {})[row["severity"]] = int(row["n"])
            typical = [_median_severity(counts, SEVERITY_ORDER) for counts in cameras.values()]
            return {
                "severity": max(typical, key=SEVERITY_ORDER.index),
                "basis": basis,
                "photos": int(photos),
            }
        with self.read() as conn:
            no_leaves = conn.execute(
                photos_since, [windows[-1][1].isoformat(timespec="milliseconds"), *([site] if site else [])]
            ).fetchone()[0]
        return {"severity": "none", "basis": "7d", "photos": 0, "no_leaves": int(no_leaves)}

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
            params.append(_through(until))
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        with self.read() as conn:
            cursor = conn.execute(
                f"""
                SELECT e.id AS event_id, e.captured_at, e.site, e.camera, e.severity,
                       e.detection_count, e.max_confidence, e.damaged_area,
                       e.model_version, e.conf_threshold, e.iou_threshold,
                       e.inference_ms, e.preview_fps, e.inference_fps,
                       e.trigger, e.synthetic, e.image_path, e.capture_id, e.camera_name,
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


def _through(until: str) -> str:
    """A date alone ("2026-11-15") means through the end of that day."""
    return f"{until}T23:59:59.999999" if len(until) == 10 else until


def _median_severity(counts: dict[str, int], order: Sequence[str]) -> str:
    """The middle severity of a set of photos; with an even count, the upper one."""
    position = sum(counts.values()) // 2
    for band in order:
        position -= counts.get(band, 0)
        if position < 0:
            return band
    return order[-1]


def _capture_cameras(conn: sqlite3.Connection, capture_ids: Sequence[str | None]) -> dict[str, list[dict]]:
    """Every camera's result in each of these photos, by capture, in camera order.

    Only for photos of more than one camera: a single camera's row already says
    everything about its photo.
    """
    wanted = sorted({c for c in capture_ids if c})
    if not wanted:
        return {}
    rows = conn.execute(
        "SELECT id, capture_id, camera, camera_name, severity, detection_count, max_confidence "
        f"FROM detection_event WHERE capture_id IN ({','.join('?' * len(wanted))}) ORDER BY camera",
        wanted,
    ).fetchall()
    found: dict[str, list[dict]] = {}
    for row in rows:
        found.setdefault(row["capture_id"], []).append(
            {key: row[key] for key in ("id", "camera", "camera_name", "severity", "detection_count", "max_confidence")}
        )
    return {capture: cameras for capture, cameras in found.items() if len(cameras) > 1}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a database created by an older version up to the current schema.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table as it was, so a
    column added later has to be added here too. The web and the worker both
    initialise, possibly at once: the loser of that race sees "duplicate
    column", which means the column is there, which is all that matters.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(detection_event)")}
    for name, definition in (
        ("camera", "INTEGER NOT NULL DEFAULT 0"),
        ("capture_id", "TEXT"),
        ("capture_lead", "INTEGER NOT NULL DEFAULT 1"),
        ("camera_name", "TEXT"),
    ):
        if name in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE detection_event ADD COLUMN {name} {definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
    # Here rather than in schema.sql, which runs before an older table has
    # these columns.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_frame_camera "
        "ON detection_event (site, camera, frame_seq, captured_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_capture ON detection_event (capture_id) "
        "WHERE capture_id IS NOT NULL"
    )
