"""Nightly maintenance: ``python -m pepperwatch.maintenance``.

Run from a systemd timer at 02:00. Two jobs, both aimed at the SD card, which
is the component most likely to fail first in this deployment:

* **Truncate the WAL.** ``wal_autocheckpoint`` only fires on a connection that
  is *writing*, so a worker idle overnight leaves the ``-wal`` file at whatever
  size the day's logging grew it to. Nothing else ever shrinks it.
* **Prune old telemetry.** One sample every 15 seconds over eight-hour cycles
  is what would otherwise grow without bound. Detection events are never
  pruned automatically — those are the research record.
"""

from __future__ import annotations

import logging

from .config import load
from .db import Database

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load()
    db = Database(cfg)
    db.initialise(writer="maintenance")

    before = db.stats()
    log.info(
        "before: %s events, %s telemetry rows, %.1f MB db, %.1f MB wal",
        before["counts"]["detection_event"],
        before["counts"]["system_log"],
        before["size_mb"],
        before["wal_mb"],
    )

    pruned = db.prune(retention_days=cfg.persist.retention_days)
    checkpoint = db.checkpoint()

    after = db.stats()
    log.info(
        "pruned %d telemetry rows older than %d days; checkpoint %s",
        pruned, cfg.persist.retention_days, checkpoint,
    )
    log.info("after:  %.1f MB db, %.1f MB wal", after["size_mb"], after["wal_mb"])

    db.log_system(
        event="maintenance",
        source="maintenance",
        detail=(
            f"pruned={pruned} wal_before={before['wal_mb']}MB "
            f"wal_after={after['wal_mb']}MB checkpointed={checkpoint['checkpointed']}"
        ),
    )
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
