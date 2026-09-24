"""WAL behaviour under a concurrent writer — manuscript §3.5.1.

§3.5.1 asks for proof that the Flask backend "successfully writes the bounding
box coordinates to the SQLite database without triggering Write-Ahead Logging
(WAL) concurrency locks." Taken literally that is not falsifiable: WAL *does*
raise ``SQLITE_BUSY`` under contention, by design. What is worth proving, and
what these tests assert, is the property the dashboard actually depends on:

1. No lock error escapes unhandled.
2. No reader blocks longer than the configured ``busy_timeout``.
3. Readers never observe a partially written event.
4. The ``-wal`` file stays bounded.

Separate *processes*, not threads, because that is the production topology:
``pepperwatch-worker`` writes while ``pepperwatch-web`` reads, and threads in
one interpreter would share a connection pool that production does not have.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from pepperwatch.config import load
from pepperwatch.db import Database

pytestmark = pytest.mark.timeout(120)

WRITE_COUNT = 400
READER_COUNT = 8
RUN_SECONDS = 8.0


def _config(db_path: Path):
    return load({"db": {"path": str(db_path)}}, use_env=False)


def _writer(db_path: str, count: int, result: mp.Queue) -> None:
    """Insert ``count`` events as fast as the lock allows."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.conftest import make_detections

    db = Database(_config(Path(db_path)))
    errors: list[str] = []
    written = 0
    started = time.monotonic()

    for seq in range(count):
        try:
            db.insert_event(
                site="Naawan",
                frame_seq=seq,
                detections=make_detections(4, seed=seq),
                model_version="stub-0",
                conf_threshold=0.25,
                iou_threshold=0.45,
                severity="moderate",
                trigger="auto",
                synthetic=True,
            )
            written += 1
        except sqlite3.OperationalError as exc:
            errors.append(f"write {seq}: {exc}")
        time.sleep(0.005)  # ~200 writes/s offered, well above field rates

    db.close()
    result.put(
        {
            "role": "writer",
            "written": written,
            "errors": errors,
            "elapsed": time.monotonic() - started,
        }
    )


def _reader(db_path: str, seconds: float, busy_timeout_s: float, result: mp.Queue) -> None:
    """Hammer the read paths the dashboard uses, checking each result."""
    db = Database(_config(Path(db_path)))
    errors: list[str] = []
    inconsistencies: list[str] = []
    slow: list[float] = []
    reads = 0
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        began = time.monotonic()
        try:
            page = db.list_events(limit=25)
            db.weekly_stats()

            # A reader must never see an event whose denormalized count
            # disagrees with its rows — that would mean the boxes and the
            # event header were not committed atomically.
            for item in page["items"]:
                event = db.get_event(int(item["id"]))
                if event is None:
                    inconsistencies.append(f"event {item['id']} vanished mid-read")
                elif event["detection_count"] != len(event["boxes"]):
                    inconsistencies.append(
                        f"event {event['id']}: count={event['detection_count']} "
                        f"boxes={len(event['boxes'])}"
                    )
            reads += 1
        except sqlite3.OperationalError as exc:
            errors.append(str(exc))

        elapsed = time.monotonic() - began
        if elapsed > busy_timeout_s:
            slow.append(elapsed)

    db.close()
    result.put(
        {
            "role": "reader",
            "reads": reads,
            "errors": errors,
            "inconsistencies": inconsistencies,
            "slow": slow,
        }
    )


@pytest.fixture
def concurrent_results(tmp_path: Path):
    """Run one writer against eight readers and collect what each saw."""
    db_path = tmp_path / "concurrent.db"
    cfg = _config(db_path)

    seed = Database(cfg)
    seed.initialise(writer="worker")
    busy_timeout_s = cfg.db.busy_timeout_ms / 1000.0
    seed.close()

    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    procs = [ctx.Process(target=_writer, args=(str(db_path), WRITE_COUNT, queue))]
    procs += [
        ctx.Process(target=_reader, args=(str(db_path), RUN_SECONDS, busy_timeout_s, queue))
        for _ in range(READER_COUNT)
    ]

    for proc in procs:
        proc.start()
    collected = [queue.get(timeout=90) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)

    return {
        "db_path": db_path,
        "writer": next(r for r in collected if r["role"] == "writer"),
        "readers": [r for r in collected if r["role"] == "reader"],
        "busy_timeout_s": busy_timeout_s,
    }


def test_journal_mode_is_wal(db: Database) -> None:
    assert db.stats()["journal_mode"] == "wal"


def test_foreign_keys_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO bounding_box (event_id, class_id, class_name, confidence, x, y, w, h) "
                "VALUES (424242, 1, 'aphid_damage', 0.5, 0.1, 0.1, 0.1, 0.1)"
            )


def test_expected_indices_exist(db: Database) -> None:
    with db.read() as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
            )
        }
    assert {
        "idx_event_captured_at",
        "idx_event_archived",
        "idx_event_severity",
        "idx_box_event",
        "idx_box_class",
        "idx_log_logged_at",
    } <= names


def test_writer_completes_without_unhandled_lock_errors(concurrent_results) -> None:
    writer = concurrent_results["writer"]
    assert writer["errors"] == [], f"writer hit lock errors: {writer['errors'][:5]}"
    assert writer["written"] == WRITE_COUNT


def test_readers_see_no_lock_errors(concurrent_results) -> None:
    failures = [error for reader in concurrent_results["readers"] for error in reader["errors"]]
    assert failures == [], f"readers hit lock errors: {failures[:5]}"


def test_readers_never_observe_a_partial_event(concurrent_results) -> None:
    """The atomicity assertion: boxes and their event header commit together."""
    problems = [
        problem
        for reader in concurrent_results["readers"]
        for problem in reader["inconsistencies"]
    ]
    assert problems == [], f"partial events observed: {problems[:5]}"


def test_no_reader_blocks_past_busy_timeout(concurrent_results) -> None:
    timeout = concurrent_results["busy_timeout_s"]
    slow = [d for reader in concurrent_results["readers"] for d in reader["slow"]]
    assert slow == [], f"reads exceeded busy_timeout of {timeout}s: {sorted(slow)[-3:]}"


def test_readers_made_progress(concurrent_results) -> None:
    """Guards against a vacuously green suite if the readers silently died."""
    reads = [reader["reads"] for reader in concurrent_results["readers"]]
    assert all(count > 0 for count in reads), f"a reader never completed a pass: {reads}"


def test_wal_is_bounded_after_checkpoint(concurrent_results, tmp_path: Path) -> None:
    db = Database(_config(concurrent_results["db_path"]))
    db.checkpoint()
    stats = db.stats()
    db.close()
    assert stats["counts"]["detection_event"] == WRITE_COUNT
    assert stats["wal_mb"] < 1.0, f"-wal did not truncate: {stats['wal_mb']} MB"
