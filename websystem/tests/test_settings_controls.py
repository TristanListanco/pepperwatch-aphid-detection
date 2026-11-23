"""Settings' processor choice and debug log."""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pytest

from pepperwatch import debuglog
from pepperwatch.mqtt_bus import Topics


@pytest.fixture(autouse=True)
def _no_debug_log_left_open(cfg):
    yield
    debuglog.stop(cfg.debug_root)
    debuglog.sync(cfg.debug_root, "test")


def _sent(monkeypatch) -> list:
    import pepperwatch.web.routes_api as api

    sent: list = []
    monkeypatch.setattr(api, "_publish_command", lambda topic, payload: sent.append((topic, payload)) or True)
    return sent


def test_the_processor_choice_is_saved_and_starts_a_new_session(app, client, monkeypatch) -> None:
    sent = _sent(monkeypatch)
    assert client.get("/api/config").get_json()["accelerator"]["mode"] == "auto"
    assert client.post("/api/accelerator", json={"mode": "gpu"}).status_code == 400
    assert client.post("/api/accelerator", json={"mode": "hailo"}).status_code == 409  # no HAT reported

    assert client.post("/api/accelerator", json={"mode": "cpu"}).status_code == 202
    assert sent == [(Topics.CMD_SESSION, {"action": "new", "label": "processor-cpu"})]
    assert client.get("/api/config").get_json()["accelerator"]["mode"] == "cpu"

    # A session that saw the HAT: now it can be chosen.
    app.config["PW_HUB"]._on_message(Topics.STATUS_SESSION, {
        "session_id": "s1",
        "accelerator": {"name": "CPU", "reason": "set to run on the CPU",
                        "hailo": {"present": True, "name": "Hailo-8L"}},
    })
    status = client.get("/api/config").get_json()["accelerator"]
    assert status["hat"] == "Hailo-8L" and status["running"] == "CPU" and status["session_id"] == "s1"
    assert client.post("/api/accelerator", json={"mode": "hailo"}).status_code == 202


def test_the_saved_processor_choice_applies_to_the_next_session(cfg) -> None:
    from pepperwatch.db import Database
    from pepperwatch.worker import apply_saved_accelerator

    apply_saved_accelerator(cfg)
    assert cfg.accelerator.mode == "auto"  # nothing saved: the config files decide
    Database(cfg).set_meta("accelerator_mode", "cpu")
    apply_saved_accelerator(cfg)
    assert cfg.accelerator.mode == "cpu"


def test_processor_and_debug_log_need_the_admin_pin(app, client) -> None:
    app.config["PW_ADMIN_PIN"] = "482913"
    assert client.post("/api/accelerator", json={"mode": "cpu"}).status_code == 403
    assert client.post("/api/debug-log", json={"action": "start"}).status_code == 403
    pin = {"X-Admin-Pin": "482913"}
    assert client.post("/api/debug-log", json={"action": "start"}, headers=pin).status_code == 200


def test_settings_starts_stops_and_downloads_a_debug_log(client) -> None:
    assert client.get("/api/debug-log").get_json() == {"active": None, "logs": []}
    started = client.post("/api/debug-log", json={"action": "start"}).get_json()
    log_id = started["active"]
    assert started["logs"][0] | {"bytes": 0} == {"id": log_id, "active": True, "bytes": 0,
                                                "started_at": started["logs"][0]["started_at"]}
    logging.getLogger("pepperwatch.web.test").debug("a line only a debug log keeps")
    assert client.post("/api/debug-log", json={"action": "stop"}).get_json()["active"] is None

    response = client.get(f"/api/debug-log/{log_id}.zip")
    assert response.status_code == 200 and response.mimetype == "application/zip"
    bundle = zipfile.ZipFile(io.BytesIO(response.data))
    text = bundle.read(f"pepperwatch-debug-{log_id}/web.log").decode()
    assert "a line only a debug log keeps" in text and "NumPy" in text
    assert client.get("/api/debug-log/nope.zip").status_code == 404
    assert client.post("/api/debug-log", json={"action": "pause"}).status_code == 400


def test_a_debug_log_stops_writing_when_it_ends_and_only_five_are_kept(tmp_path: Path) -> None:
    root = tmp_path / "debug"
    for day in range(1, 8):
        (root / f"202611{day:02d}T000000Z").mkdir(parents=True)
    log_id = debuglog.start(root)
    assert debuglog.start(root) == log_id  # one at a time
    debuglog.sync(root, "worker")
    worker = logging.getLogger("pepperwatch.worker")
    worker.debug("frame 7: 2 boxes")
    debuglog.stop(root)
    debuglog.sync(root, "worker")
    worker.debug("after the end")

    text = (root / log_id / "worker.log").read_text()
    assert "frame 7: 2 boxes" in text and "ends here" in text and "after the end" not in text
    assert logging.getLogger("pepperwatch").level == logging.NOTSET  # back to INFO via the root logger
    kept = [entry["id"] for entry in debuglog.logs(root)]
    assert kept == [log_id, "20261107T000000Z", "20261106T000000Z", "20261105T000000Z", "20261104T000000Z"]
    assert debuglog.archive(root, "../etc") is None
