"""The session endpoints: list, download, build the cross-session CSV."""

from __future__ import annotations

import csv
import io

import pytest

from pepperwatch.session import SessionRecorder

META = {"accelerator": {"kind": "cpu", "name": "CPU", "profile": "cpu", "reason": "test"}}


@pytest.fixture
def recorded(cfg):
    recorder = SessionRecorder(cfg.sessions_root, META)
    recorder.record_inference({"frame_seq": 1, "infer_ms": 80.0, "total_ms": 85.0})
    recorder.record_system({"power_w": 4.2, "power_source": "pmic"})
    recorder.close()
    return recorder


def test_sessions_are_listed_with_their_files(client, recorded) -> None:
    body = client.get("/api/sessions").get_json()
    assert [s["session_id"] for s in body["sessions"]] == [recorded.session_id]
    assert set(body["sessions"][0]["files"]) == {"inference.csv", "system.csv", "summary.json", "session.json"}


def test_a_session_file_downloads(client, recorded) -> None:
    response = client.get(f"/api/sessions/{recorded.session_id}/inference.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert rows[0]["infer_ms"] == "80.0"


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/{sid}/../../db/test.db",
        "/api/sessions/{sid}/notes.txt",
        "/api/sessions/..%2F..%2Fdb/inference.csv",
        "/api/sessions/does-not-exist/inference.csv",
    ],
)
def test_only_whitelisted_session_files_are_served(client, recorded, path) -> None:
    assert client.get(path.format(sid=recorded.session_id)).status_code == 404


def test_the_cross_session_csv_is_built_on_request(client, recorded) -> None:
    response = client.get("/api/sessions.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert rows[0]["session_id"] == recorded.session_id
    assert rows[0]["power_source"] == "pmic"


def test_no_session_is_reported_without_a_worker(client) -> None:
    assert client.get("/api/session").get_json() == {"session": None, "summary": None}


def test_a_new_session_needs_the_worker(client) -> None:
    response = client.post("/api/session/new", json={"label": "row 3"})
    assert response.status_code == 503
