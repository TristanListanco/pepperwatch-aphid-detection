"""Shared fixtures.

Every test gets its own database file under ``tmp_path``. None of them touch
``var/db/pepperwatch.db``, so running the suite never disturbs a running dev
instance or a device's field data.
"""

from __future__ import annotations

import os
import random
import uuid
from pathlib import Path

import pytest

from pepperwatch.config import Config, load
from pepperwatch.db import Database
from pepperwatch.models import Detection


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Config pointed at throwaway storage, isolated from any running instance.

    ``use_env=False`` so a developer's exported PW_* variables cannot change
    what the suite asserts.

    The per-test shared-memory name matters as much as the temp database. POSIX
    shm is machine-global, so a test using the default name would attach to a
    dev worker's live segment and read its frames -- which silently turned the
    "no worker running" assertion green or red depending on what happened to be
    running.
    """
    unique = f"pw_t_{os.getpid() % 100000}_{uuid.uuid4().hex[:8]}"
    return load(
        {
            "db": {"path": str(tmp_path / "test.db")},
            "media": {"root": str(tmp_path / "media")},
            "sessions": {"root": str(tmp_path / "sessions")},
            "frames": {
                "shm_name": unique,
                "tmpfs_dir": str(tmp_path / "frames"),
            },
        },
        use_env=False,
    )


@pytest.fixture
def db(cfg: Config) -> Database:
    database = Database(cfg)
    database.initialise(writer="worker")
    yield database
    database.close()


@pytest.fixture
def detections() -> list[Detection]:
    """A small, fixed set of overlapping boxes."""
    return [
        Detection(1, "aphid_damage", 0.91, 0.10, 0.10, 0.20, 0.20),
        Detection(1, "aphid_damage", 0.77, 0.15, 0.15, 0.20, 0.20),
        Detection(0, "healthy", 0.64, 0.60, 0.60, 0.25, 0.25),
    ]


def make_detections(count: int, *, seed: int = 0) -> list[Detection]:
    """``count`` pseudo-random aphid boxes, deterministic in ``seed``."""
    rng = random.Random(seed)
    return [
        Detection(
            class_id=1,
            class_name="aphid_damage",
            confidence=round(rng.uniform(0.3, 0.95), 3),
            x=round(rng.uniform(0.0, 0.7), 3),
            y=round(rng.uniform(0.0, 0.7), 3),
            w=round(rng.uniform(0.05, 0.25), 3),
            h=round(rng.uniform(0.05, 0.25), 3),
        )
        for _ in range(count)
    ]


@pytest.fixture
def app(cfg):
    """A Flask app with the MQTT hub left unstarted.

    Tests that need bus behaviour drive the hub directly rather than standing up
    a broker, so the suite stays hermetic and does not depend on mosquitto
    being installed on whatever machine runs it.
    """
    from pepperwatch.web import create_app

    application = create_app(cfg, start_hub=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded_db(app, cfg):
    """An app whose database already holds a spread of events.

    Covers every severity band, an archived event, and telemetry, so the history
    filters and the KPI tiles have something meaningful to return.
    """
    from pepperwatch.db import Database

    database = app.config["PW_DB"]
    bands = ["none", "low", "low", "moderate", "moderate", "high", "high", "high"]
    ids = []
    for index, severity in enumerate(bands):
        count = {"none": 0, "low": 2, "moderate": 4, "high": 8}[severity]
        ids.append(
            database.insert_event(
                site=cfg.site.name,
                frame_seq=index,
                detections=make_detections(count, seed=index),
                model_version="stub-heuristic-1",
                conf_threshold=0.25,
                iou_threshold=0.45,
                severity=severity,
                trigger="auto",
                image_path=f"2026/09/22/frame{index}.jpg",
                thumb_path=f"2026/09/22/frame{index}_thumb.jpg",
                inference_ms=100.0 + index,
                preview_fps=25.0,
                inference_fps=8.0,
                synthetic=True,
            )
        )
    database.set_archived(ids[0], True)

    for index in range(4):
        database.log_system(
            event="telemetry",
            source="worker",
            telemetry={
                "cpu_temp_c": 55.0 + index,
                "cpu_percent": 42.0,
                "mem_used_mb": 1800.0,
                "mem_total_mb": 8192.0,
                "power_w": 5.4,
                "synthetic": True,
            },
        )

    return {"app": app, "db": database, "ids": ids, "bands": bands}
