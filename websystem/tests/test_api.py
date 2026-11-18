"""The REST API surface.

The headline test here is ``test_mock_json_payload_writes_bounding_boxes``,
which is manuscript §3.5.1's stated requirement almost word for word: send mock
JSON payloads to the API endpoints, assert an HTTP success code, and assert the
bounding box coordinates reached SQLite.
"""

from __future__ import annotations

import csv
import io

import pytest

GET_ROUTES = [
    "/api/health",
    "/api/detections/latest",
    "/api/pipeline",
    "/api/events",
    "/api/stats/weekly",
    "/api/recommendations",
    "/api/recommendations/high",
    "/api/config",
    "/api/export.csv",
    "/stream/status",
]


# -- reachability ----------------------------------------------------------


@pytest.mark.parametrize("route", GET_ROUTES)
def test_every_get_route_succeeds(client, route) -> None:
    """§3.5.1: assert the server returns standard HTTP success codes."""
    response = client.get(route)
    assert response.status_code == 200, f"{route} returned {response.status_code}"


def test_health_reports_provenance(client) -> None:
    body = client.get("/api/health").get_json()
    assert body["status"] == "ok"
    # No worker in the test app, so no session has been announced.
    assert body["session"] is None
    assert "database" in body and body["database"]["journal_mode"] == "wal"
    # The advisory content is an unvalidated draft; the API must say so.
    assert body["advisory_validated"] is False


def test_health_reports_the_worker_as_down_when_it_is(client) -> None:
    """Honest liveness: no worker has published, so nothing may claim it is up."""
    worker = client.get("/api/health").get_json()["worker"]
    assert worker["alive"] is False


# -- the §3.5.1 ingest assertion ------------------------------------------


def test_mock_json_payload_writes_bounding_boxes(client, app) -> None:
    """§3.5.1, verbatim: mock JSON in, HTTP success out, boxes in SQLite."""
    payload = {
        "frame_seq": 4242,
        "severity": "moderate",
        "model_version": "test-1",
        "boxes": [
            {"class_id": 1, "class_name": "aphid_damage", "confidence": 0.81,
             "x": 0.10, "y": 0.20, "w": 0.30, "h": 0.40},
            {"class_id": 1, "class_name": "aphid_damage", "confidence": 0.55,
             "x": 0.50, "y": 0.60, "w": 0.15, "h": 0.15},
        ],
    }

    response = client.post("/api/detections", json=payload)
    assert response.status_code == 201
    event_id = response.get_json()["event_id"]

    stored = app.config["PW_DB"].get_event(event_id)
    assert stored is not None
    assert stored["detection_count"] == 2
    assert stored["severity"] == "moderate"

    # Coordinates must survive the round trip exactly.
    best = max(stored["boxes"], key=lambda b: b["confidence"])
    assert best["confidence"] == pytest.approx(0.81)
    assert best["x"] == pytest.approx(0.10)
    assert best["y"] == pytest.approx(0.20)
    assert best["w"] == pytest.approx(0.30)
    assert best["h"] == pytest.approx(0.40)


def test_ingest_derives_severity_when_omitted(client, app) -> None:
    response = client.post(
        "/api/detections",
        json={
            "frame_seq": 1,
            "boxes": [
                {"class_id": 1, "class_name": "aphid_damage", "confidence": 0.9,
                 "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}
            ],
        },
    )
    assert response.status_code == 201
    # A single box covering 25% of the frame is high by area.
    assert response.get_json()["severity"] == "high"


def test_ingest_accepts_a_clean_scan(client) -> None:
    response = client.post("/api/detections", json={"frame_seq": 2, "boxes": []})
    assert response.status_code == 201
    assert response.get_json()["severity"] == "none"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, "frame_seq"),
        ({"frame_seq": "abc"}, "frame_seq"),
        ({"frame_seq": 1, "boxes": "nope"}, "boxes must be a list"),
        ({"frame_seq": 1, "boxes": [{"class_id": 1}]}, "invalid"),
        (
            {"frame_seq": 1, "boxes": [
                {"class_id": 1, "class_name": "aphid_damage", "confidence": 1.7,
                 "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}
            ]},
            "confidence",
        ),
        (
            {"frame_seq": 1, "boxes": [
                {"class_id": 1, "class_name": "aphid_damage", "confidence": 0.5,
                 "x": 0.1, "y": 0.1, "w": 0.0, "h": 0.1}
            ]},
            "positive width",
        ),
        ({"frame_seq": 1, "boxes": [], "severity": "catastrophic"}, "unknown severity"),
    ],
)
def test_ingest_rejects_malformed_payloads(client, payload, expected) -> None:
    """Bad input must fail loudly rather than storing nonsense."""
    response = client.post("/api/detections", json=payload)
    assert response.status_code == 400
    assert expected in response.get_json()["error"]


def test_ingest_is_idempotent_on_a_retry(client) -> None:
    """A retried publish must not duplicate the event."""
    payload = {"frame_seq": 77, "captured_at": "2026-09-22T04:00:00.000+00:00", "boxes": []}
    first = client.post("/api/detections", json=payload).get_json()["event_id"]
    second = client.post("/api/detections", json=payload).get_json()["event_id"]
    assert first == second


# -- history ---------------------------------------------------------------


def test_events_excludes_archived_by_default(seeded_db, client) -> None:
    body = client.get("/api/events").get_json()
    assert body["total"] == 7  # eight seeded, one archived
    assert all(item["archived"] == 0 for item in body["items"])


def test_events_can_include_archived(seeded_db, client) -> None:
    assert client.get("/api/events?archived=all").get_json()["total"] == 8
    assert client.get("/api/events?archived=true").get_json()["total"] == 1


def test_events_filters_by_severity(seeded_db, client) -> None:
    assert client.get("/api/events?severity=high").get_json()["total"] == 3
    assert client.get("/api/events?severity=moderate").get_json()["total"] == 2


def test_events_filters_by_class(seeded_db, client) -> None:
    body = client.get("/api/events?class=aphid_damage").get_json()
    # The one archived event is also the only clean one, so all seven
    # remaining events carry aphid_damage boxes.
    assert body["total"] == 7
    # Including archived adds the clean event, which has no boxes to match.
    assert client.get("/api/events?archived=all&class=aphid_damage").get_json()["total"] == 7


def test_events_paginates_with_a_stable_total(seeded_db, client) -> None:
    first = client.get("/api/events?limit=3&offset=0").get_json()
    second = client.get("/api/events?limit=3&offset=3").get_json()
    assert first["total"] == second["total"] == 7
    assert len(first["items"]) == 3
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


def test_events_returns_newest_first(seeded_db, client) -> None:
    items = client.get("/api/events?limit=10").get_json()["items"]
    stamps = [item["captured_at"] for item in items]
    assert stamps == sorted(stamps, reverse=True)


def test_events_rejects_a_non_integer_limit(client) -> None:
    assert client.get("/api/events?limit=lots").status_code == 400


def test_single_event_includes_boxes_and_advice(seeded_db, client) -> None:
    event_id = seeded_db["ids"][-1]
    body = client.get(f"/api/events/{event_id}").get_json()
    assert body["id"] == event_id
    assert len(body["boxes"]) == body["detection_count"] == 8
    assert body["advice"]["urgency"] == "urgent"


def test_missing_event_is_a_404(client) -> None:
    assert client.get("/api/events/999999").status_code == 404


def test_archive_toggles_and_reports(seeded_db, client) -> None:
    event_id = seeded_db["ids"][3]
    assert client.post(f"/api/events/{event_id}/archive", json={"archived": True}).status_code == 200
    assert client.get("/api/events").get_json()["total"] == 6
    client.post(f"/api/events/{event_id}/archive", json={"archived": False})
    assert client.get("/api/events").get_json()["total"] == 7


# -- KPI tiles and advice --------------------------------------------------


def test_weekly_stats_shape_matches_the_wireframe_tiles(seeded_db, client) -> None:
    """Figure 3.4 draws four tiles and a sentence. All must be served."""
    body = client.get("/api/stats/weekly").get_json()
    for key in (
        "events", "detections", "delta_pct", "avg_confidence",
        "trend", "severity", "distribution", "insight", "advice",
    ):
        assert key in body, f"missing {key}"

    assert body["events"] == 8
    assert body["severity"] == "high"
    assert body["trend"] in ("rising", "falling", "stable")
    assert body["distribution"]["aphid_damage"] == 2 + 2 + 4 + 4 + 8 + 8 + 8
    assert body["site"] == "Naawan"


def test_weekly_trend_follows_aphid_damage_not_every_box(app, cfg, client) -> None:
    """Scanning more healthy leaves is not a rising trend, and damage with none
    the week before has no percentage to show."""
    from datetime import datetime, timedelta, timezone

    from pepperwatch.models import Detection

    database = app.config["PW_DB"]
    last_week = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="milliseconds")

    seq = iter(range(100))

    def scan(healthy: int, damaged: int, captured_at: str | None = None) -> None:
        boxes = [
            Detection(class_id=c, class_name=n, confidence=0.8, x=0.1, y=0.1, w=0.1, h=0.1)
            for c, n, k in ((0, "healthy", healthy), (1, "aphid_damage", damaged))
            for _ in range(k)
        ]
        database.insert_event(
            site=cfg.site.name, frame_seq=next(seq), detections=boxes, model_version="t",
            conf_threshold=0.25, iou_threshold=0.45, severity="low", captured_at=captured_at,
        )

    scan(healthy=2, damaged=0, captured_at=last_week)
    scan(healthy=40, damaged=0)
    body = client.get("/api/stats/weekly").get_json()
    assert body["trend"] == "stable" and body["delta_pct"] == 0.0

    scan(healthy=0, damaged=3)
    body = client.get("/api/stats/weekly").get_json()
    assert body["damaged"] == 3 and body["damaged_previous"] == 0
    assert body["delta_pct"] is None and body["trend"] == "rising"
    assert "No aphid damage was recorded in the previous 7 days" in body["insight"]

    scan(healthy=0, damaged=1, captured_at=last_week)
    body = client.get("/api/stats/weekly").get_json()
    assert body["delta_pct"] == 200.0 and body["trend"] == "rising"


def test_weekly_insight_is_a_plain_sentence(seeded_db, client) -> None:
    insight = client.get("/api/stats/weekly").get_json()["insight"]
    assert "Naawan" in insight
    assert insight.endswith(".")
    assert "detections were recorded" in insight


def test_stats_rejects_a_bad_days_parameter(client) -> None:
    assert client.get("/api/stats/weekly?days=forever").status_code == 400


@pytest.mark.parametrize(
    "severity,urgency",
    [("none", "routine"), ("low", "monitor"), ("moderate", "act_soon"), ("high", "urgent")],
)
def test_recommendations_cover_every_severity_band(client, severity, urgency) -> None:
    body = client.get(f"/api/recommendations/{severity}").get_json()
    assert body["urgency"] == urgency
    assert body["headline"]
    assert body["actions"]
    # The content is a draft until an agriculturist signs it off.
    assert body["validated"] is False


def test_recommendations_are_available_in_cebuano(client) -> None:
    """'Localized' in Northern Mindanao will be read as translated."""
    english = client.get("/api/recommendations/high?locale=en").get_json()
    cebuano = client.get("/api/recommendations/high?locale=ceb").get_json()
    assert cebuano["locale"] == "ceb"
    assert cebuano["headline"] != english["headline"]
    assert len(cebuano["actions"]) == len(english["actions"])


def test_unknown_locale_falls_back_to_english(client) -> None:
    body = client.get("/api/recommendations/high?locale=klingon").get_json()
    assert body["locale"] == "en"


# -- export ----------------------------------------------------------------


def test_csv_export_has_the_expected_header_and_rows(seeded_db, client) -> None:
    response = client.get("/api/export.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert ".csv" in response.headers["Content-Disposition"]

    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    assert rows[0][:4] == ["event_id", "captured_at", "site", "severity"]

    # One row per box, plus one for the clean event that has none.
    expected = (2 + 2 + 4 + 4 + 8 + 8 + 8) + 1
    assert len(rows) - 1 == expected


def test_csv_export_of_an_empty_database_is_still_valid(client) -> None:
    rows = list(csv.reader(io.StringIO(client.get("/api/export.csv").get_data(as_text=True))))
    assert len(rows) == 1  # header only
    assert rows[0][0] == "event_id"


_FRAMES = iter(range(10_000, 20_000))  # (site, frame, time) is unique


def _event(database, severity, *, camera=0, hours_ago=1.0, synthetic=False, leaves=1) -> None:
    from datetime import datetime, timedelta, timezone

    from pepperwatch.models import Detection

    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    seen = [Detection(class_id=0, class_name="healthy", confidence=0.9, x=0.5, y=0.5, w=0.1, h=0.1)] * leaves
    database.insert_event(
        site="Naawan", camera=camera, frame_seq=next(_FRAMES), detections=seen, model_version="m",
        conf_threshold=0.25, iou_threshold=0.45, severity=severity, trigger="auto",
        captured_at=at.isoformat(timespec="milliseconds"), image_path=None, thumb_path=None,
        inference_ms=1.0, preview_fps=1.0, inference_fps=1.0, synthetic=synthetic,
    )


def test_one_false_alarm_does_not_set_the_farmers_status(app) -> None:
    database = app.config["PW_DB"]
    for _ in range(5):
        _event(database, "none")
    _event(database, "high")  # the green wall, once
    assert database.recent_status() == {"severity": "none", "basis": "24h", "photos": 6}


def test_the_camera_whose_plants_look_worst_sets_the_status(app) -> None:
    database = app.config["PW_DB"]
    for _ in range(4):
        _event(database, "none", camera=0)
        _event(database, "moderate", camera=1)
    assert database.recent_status()["severity"] == "moderate"


def test_the_status_falls_back_to_the_week_and_ignores_the_stub(app) -> None:
    database = app.config["PW_DB"]
    _event(database, "high", hours_ago=0.5, synthetic=True)  # stub detector: not a measurement
    _event(database, "low", hours_ago=72)
    assert database.recent_status() == {"severity": "low", "basis": "7d", "photos": 1}
    assert database.weekly_stats()["events"] == 1


def test_photos_with_no_leaves_say_nothing_about_the_plants(app) -> None:
    database = app.config["PW_DB"]
    for _ in range(5):
        _event(database, "none", leaves=0)  # pointed at the sky
    assert database.recent_status() == {"severity": "none", "basis": "7d", "photos": 0, "no_leaves": 5}

    # Leaves seen earlier in the week still answer, on the week's basis.
    _event(database, "low", hours_ago=72)
    assert database.recent_status() == {"severity": "low", "basis": "7d", "photos": 1}


def test_weekly_stats_carry_the_farmers_status_and_the_stub_flag(app, client) -> None:
    _event(app.config["PW_DB"], "low")
    app.config["PW_HUB"]._on_message("pepperwatch/status/detector", {"synthetic": True})
    stats = client.get("/api/stats/weekly?locale=ceb").get_json()
    assert stats["status"]["severity"] == "low" and stats["status"]["advice"]["locale"] == "ceb"
    assert stats["detector_synthetic"] is True


def test_zip_export_has_the_csv_and_every_saved_photo(seeded_db, client, cfg) -> None:
    import zipfile

    for index in (0, 1):  # two photos on disk; the other events' are missing
        photo = cfg.media_root / f"2026/09/22/frame{index}.jpg"
        photo.parent.mkdir(parents=True, exist_ok=True)
        photo.write_bytes(b"\xff\xd8jpeg " + bytes([index]) + b"\xff\xd9")

    response = client.get("/api/export.zip")
    assert response.status_code == 200 and response.mimetype == "application/zip"
    assert ".zip" in response.headers["Content-Disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    assert archive.testzip() is None
    assert sorted(archive.namelist()) == [
        "detections.csv", "images/2026/09/22/frame0.jpg", "images/2026/09/22/frame1.jpg",
    ]
    assert archive.read("images/2026/09/22/frame1.jpg") == b"\xff\xd8jpeg \x01\xff\xd9"
    rows = list(csv.DictReader(io.StringIO(archive.read("detections.csv").decode())))
    assert len(rows) == (2 + 2 + 4 + 4 + 8 + 8 + 8) + 1  # the same rows as the CSV export
    linked = {row["image_path"] for row in rows}
    assert linked == {"images/2026/09/22/frame0.jpg", "images/2026/09/22/frame1.jpg", ""}
    assert all(name in archive.namelist() for name in linked - {""})


def test_zip_export_of_an_empty_database_has_just_the_header(client) -> None:
    import zipfile

    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export.zip").get_data()))
    assert archive.namelist() == ["detections.csv"]
    assert archive.read("detections.csv").decode().startswith("event_id,")


# -- control ---------------------------------------------------------------


def test_capture_reports_503_when_the_worker_is_unreachable(client) -> None:
    """The bus is down in tests, so this is the honest answer."""
    response = client.post("/api/capture", json={})
    assert response.status_code == 503
    assert "worker unreachable" in response.get_json()["error"]


def test_inference_control_validates_the_action(client) -> None:
    assert client.post("/api/inference/pause").status_code == 503  # valid, bus down
    assert client.post("/api/inference/explode").status_code == 400


def _two_cameras(app, on=None, connected=None) -> None:
    hub = app.config["PW_HUB"]
    hub._on_message("pepperwatch/status/session", {"cameras": [{"index": 0}, {"index": 1}]})
    if on is not None:
        hub._on_message("pepperwatch/telemetry/pipeline", {"camera_on": on, "camera_connected": connected})


def test_camera_switch_validates_before_forwarding(app, client) -> None:
    _two_cameras(app)
    assert client.post("/api/camera/1", json={"enabled": "no"}).status_code == 400
    assert client.post("/api/camera/2", json={"enabled": False}).status_code == 404
    assert client.post("/api/camera/1", json={"enabled": False}).status_code == 503  # valid, bus down
    # A one-camera session's telemetry still cached when two cameras start.
    _two_cameras(app, on=[True])
    assert client.post("/api/camera/1", json={"enabled": False}).status_code == 503


def test_the_last_camera_on_cannot_be_turned_off(app, client, monkeypatch) -> None:
    import pepperwatch.web.routes_api as api

    sent = []
    monkeypatch.setattr(api, "_publish_command", lambda topic, payload: sent.append((topic, payload)) or True)
    _two_cameras(app, on=[True, False])
    assert client.post("/api/camera/0", json={"enabled": False}).status_code == 409
    assert client.post("/api/camera/1", json={"enabled": True}).status_code == 202
    assert sent == [("pepperwatch/cmd/camera", {"camera": 1, "enabled": True})]


def test_an_unplugged_camera_cannot_be_turned_on_and_does_not_count_as_on(app, client, monkeypatch) -> None:
    import pepperwatch.web.routes_api as api

    sent = []
    monkeypatch.setattr(api, "_publish_command", lambda topic, payload: sent.append((topic, payload)) or True)
    _two_cameras(app, on=[True, True], connected=[True, False])
    response = client.post("/api/camera/1", json={"enabled": True})
    assert response.status_code == 409 and "unplugged" in response.get_json()["error"]
    # Camera 2 is unplugged, so camera 1 is the last one running.
    assert client.post("/api/camera/0", json={"enabled": False}).status_code == 409
    assert sent == []
    assert client.get("/api/session").get_json()["camera_connected"] == [True, False]


def test_clearing_data_and_thresholds_need_the_admin_pin(app, client) -> None:
    app.config["PW_ADMIN_PIN"] = "482913"
    body = {"confirm": "DELETE"}
    assert client.post("/api/data/clear", json=body).status_code == 403
    assert client.patch("/api/config", json={"conf_threshold": 0.4}).status_code == 403
    assert client.patch("/api/config", json={"heartbeat_detected_s": 60}).status_code == 403
    wrong = client.post("/api/data/clear", json=body, headers={"X-Admin-Pin": "000000"})
    assert wrong.status_code == 403 and "wrong" in wrong.get_json()["error"]
    right = client.post("/api/data/clear", json=body, headers={"X-Admin-Pin": "482913"})
    assert right.status_code == 200
    # Viewing stays open.
    assert client.get("/api/config").status_code == 200


def test_guessing_the_pin_is_locked_out_after_five_tries(app, client) -> None:
    app.config["PW_ADMIN_PIN"] = "482913"
    for guess in range(5):
        client.post("/api/data/clear", json={"confirm": "DELETE"}, headers={"X-Admin-Pin": f"{guess:06d}"})
    locked = client.post("/api/data/clear", json={"confirm": "DELETE"}, headers={"X-Admin-Pin": "482913"})
    assert locked.status_code == 429


def test_polling_pages_get_every_cameras_detections(app, client) -> None:
    hub = app.config["PW_HUB"]
    hub._on_message("pepperwatch/detect/frame", {"camera": 0, "frame_seq": 1, "boxes": [], "severity": "none"})
    hub._on_message("pepperwatch/detect/frame", {"camera": 1, "frame_seq": 9, "boxes": [], "severity": "low"})
    body = client.get("/api/detections/latest").get_json()
    assert body["camera"] == 1  # the newest, as before
    assert [frame["camera"] for frame in body["cameras"]] == [0, 1]


def test_one_off_events_are_not_replayed_to_pages_that_load_later(app) -> None:
    """A page loading after a severity flag or a saved photo must not toast it again."""
    hub = app.config["PW_HUB"]
    hub._on_message("pepperwatch/detect/flag", {"severity": "high"})
    hub._on_message("pepperwatch/event/persisted", {"event_id": 1, "trigger": "manual"})
    hub._on_message("pepperwatch/telemetry/system", {"cpu_temp_c": 50})
    assert set(hub.snapshot()["latest"]) == {"telemetry/system"}


def test_a_to_date_includes_that_whole_day(seeded_db, client) -> None:
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    everything = client.get("/api/events?archived=all&limit=100").get_json()["total"]
    assert client.get(f"/api/events?archived=all&limit=100&to={today}").get_json()["total"] == everything
    rows = list(csv.reader(io.StringIO(client.get(f"/api/export.csv?to={today}").get_data(as_text=True))))
    assert len(rows) > 1


def test_config_exposes_only_the_tunables(client) -> None:
    body = client.get("/api/config").get_json()
    assert set(body) == {"site", "inference", "persist", "severity", "web", "locales", "advisory"}
    # Secrets must never appear.
    assert "secret_key" not in body["web"]


def test_config_default_threshold_is_not_figure_3_3s_value(client) -> None:
    """Documents the deliberate departure from Figure 3.3's 'Confidence Filter (80)'."""
    assert client.get("/api/config").get_json()["inference"]["conf_threshold"] == 0.25


def _worker_that_applies(app, monkeypatch) -> list:
    """A worker on the other end of the bus: it applies the thresholds and
    reports them in its next pipeline telemetry, as the real one does."""
    import pepperwatch.web.routes_api as api

    sent = []
    hub = app.config["PW_HUB"]
    reported = {"conf_threshold": 0.25, "iou_threshold": 0.45}

    def publish(topic, payload):
        sent.append((topic, payload))
        reported.update(payload)
        hub._on_message("pepperwatch/telemetry/pipeline", dict(reported))
        return True

    monkeypatch.setattr(api, "_publish_command", publish)
    return sent


def test_patch_config_is_applied_once_the_detector_reports_it(app, client, monkeypatch) -> None:
    sent = _worker_that_applies(app, monkeypatch)
    response = client.patch("/api/config", json={"conf_threshold": 0.4})
    assert response.status_code == 200
    assert response.get_json() == {"updated": {"conf_threshold": 0.4}, "applied": True}
    assert sent == [("pepperwatch/cmd/config", {"conf_threshold": 0.4})]
    assert client.get("/api/config").get_json()["inference"]["conf_threshold"] == 0.4


def test_patch_config_says_so_when_the_detector_is_unreachable(client) -> None:
    response = client.patch("/api/config", json={"conf_threshold": 0.4})
    assert response.status_code == 503  # no broker in tests
    assert client.get("/api/config").get_json()["inference"]["conf_threshold"] == 0.25


def test_patch_config_is_not_applied_without_the_detectors_confirmation(app, client, monkeypatch) -> None:
    import pepperwatch.web.routes_api as api

    monkeypatch.setattr(api, "_publish_command", lambda topic, payload: True)  # sent, but no worker
    monkeypatch.setattr(api, "CONFIRM_TIMEOUT_S", 0.2)
    response = client.patch("/api/config", json={"iou_threshold": 0.6})
    assert response.status_code == 504
    assert client.get("/api/config").get_json()["inference"]["iou_threshold"] == 0.45


def test_config_shows_the_thresholds_the_detector_reports(app, client) -> None:
    """A new session starts again from the config files; show what is running."""
    app.config["PW_HUB"]._on_message(
        "pepperwatch/telemetry/pipeline", {"conf_threshold": 0.35, "iou_threshold": 0.5}
    )
    inference = client.get("/api/config").get_json()["inference"]
    assert (inference["conf_threshold"], inference["iou_threshold"]) == (0.35, 0.5)


@pytest.mark.parametrize("value", [0.0, 1.0, -0.5, 1.5, "high"])
def test_patch_config_rejects_out_of_range_thresholds(client, value) -> None:
    assert client.patch("/api/config", json={"conf_threshold": value}).status_code == 400


def test_photo_interval_is_applied_once_the_worker_reports_it(app, client, monkeypatch) -> None:
    sent = _worker_that_applies(app, monkeypatch)
    response = client.patch("/api/config", json={"heartbeat_detected_s": 60})
    assert response.status_code == 200 and response.get_json()["applied"] is True
    assert sent == [("pepperwatch/cmd/config", {"heartbeat_detected_s": 60.0})]
    persist = client.get("/api/config").get_json()["persist"]
    assert persist == {"heartbeat_detected_s": 60.0, "heartbeat_clean_s": 300}


@pytest.mark.parametrize("value", [0, 5, 86401, "often", float("nan")])
def test_patch_config_rejects_impossible_photo_intervals(client, value) -> None:
    assert client.patch("/api/config", json={"heartbeat_clean_s": value}).status_code == 400


def test_anyone_can_rename_the_site_and_it_outlasts_a_restart(app, client, cfg) -> None:
    from pepperwatch.config import load
    from pepperwatch.web import create_app

    app.config["PW_ADMIN_PIN"] = "482913"  # set, but not asked for: it is the farm's own name
    response = client.patch("/api/site", json={"name": "  Buru-un  "})
    assert response.status_code == 200 and response.get_json() == {"name": "Buru-un"}
    assert client.get("/api/config").get_json()["site"]["name"] == "Buru-un"
    assert "<h1 data-site-name>Buru-un</h1>" in client.get("/").get_data(as_text=True)
    with app.config["PW_DB"].read() as conn:
        logged = conn.execute("SELECT detail FROM system_log WHERE event = 'site_renamed'").fetchall()
    assert [row["detail"] for row in logged] == ["Naawan -> Buru-un"]

    # A restart reads the saved name, not the config file's.
    fresh = load(
        {
            "db": {"path": cfg.db.path},
            "media": {"root": str(cfg.media_root)},
            "frames": {"shm_name": cfg.frames.shm_name, "tmpfs_dir": cfg.frames.tmpfs_dir},
        },
        use_env=False,
    )
    assert fresh.site.name == "Naawan"
    create_app(fresh, start_hub=False)
    assert fresh.site.name == "Buru-un"


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "Farm\nName", None, 42])
def test_a_site_name_must_be_one_short_line(client, name) -> None:
    assert client.patch("/api/site", json={"name": name}).status_code == 400
    assert client.get("/api/config").get_json()["site"]["name"] == "Naawan"


def test_patch_config_rejects_an_empty_body(client) -> None:
    assert client.patch("/api/config", json={}).status_code == 400


# -- clock -----------------------------------------------------------------


def test_time_sync_rejects_a_backward_jump(client) -> None:
    """A client with a bad clock must not be able to rewrite history."""
    body = client.post("/api/time", json={"iso": "2020-01-01T00:00:00+00:00"}).get_json()
    assert body["accepted"] is False
    assert body["reason"] == "not a forward jump"


def test_time_sync_ignores_trivial_drift(client) -> None:
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    assert client.post("/api/time", json={"iso": soon}).get_json()["reason"] == "within tolerance"


def test_time_sync_records_a_large_forward_jump(client, app) -> None:
    from datetime import datetime, timedelta, timezone

    later = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    body = client.post("/api/time", json={"iso": later}).get_json()
    assert body["accepted"] is True
    assert body["applied"] is False  # needs the privileged helper
    assert "RTC battery" in body["hint"]

    logged = app.config["PW_DB"].connection().execute(
        "SELECT COUNT(*) AS n FROM system_log WHERE event = 'time_sync_offered'"
    ).fetchone()["n"]
    assert logged == 1


def test_time_sync_rejects_garbage(client) -> None:
    assert client.post("/api/time", json={"iso": "not a date"}).status_code == 400
    assert client.post("/api/time", json={}).status_code == 400


# -- security ------------------------------------------------------------------


def test_oversized_requests_are_refused(client) -> None:
    assert client.post("/api/detections", data="x" * (2 * 1024 * 1024),
                       content_type="application/json").status_code == 413


def test_ingest_rejects_unknown_class_names(client) -> None:
    """Class names are drawn into the charts; arbitrary ones would be stored XSS."""
    box = {"class_id": 1, "class_name": "<img src=x onerror=alert(1)>", "confidence": 0.5,
           "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}
    assert client.post("/api/detections", json={"frame_seq": 1, "boxes": [box]}).status_code == 400


@pytest.mark.parametrize("value,expected", [
    ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"), ("+1", "'+1"), ("-cmd", "'-cmd"),
    ("@SUM(A1)", "'@SUM(A1)"), ("row 3", "row 3"), (-1.5, -1.5), (None, None),
])
def test_csv_cells_cannot_become_formulas(value, expected) -> None:
    from pepperwatch.session import csv_safe

    assert csv_safe(value) == expected


def test_media_route_rejects_a_sibling_directory(client, cfg) -> None:
    sibling = cfg.media_root.parent / (cfg.media_root.name + "-old")
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "secret.jpg").write_bytes(b"x")
    assert client.get(f"/media/../{sibling.name}/secret.jpg").status_code == 404


def test_a_database_from_before_cameras_gains_the_camera_column(cfg) -> None:
    """The Pi's existing database has no camera column; startup adds it, old
    events read as camera 0, and new ones keep the camera they came from."""
    import sqlite3

    from pepperwatch.db import SCHEMA_PATH, Database
    from pepperwatch.models import Detection

    # Today's schema without the camera column is what an older install has.
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(cfg.db_path)
    old.executescript(SCHEMA_PATH.read_text())
    old.execute("ALTER TABLE detection_event DROP COLUMN camera")
    old.execute(
        "INSERT INTO detection_event (captured_at, site, frame_seq, model_version,"
        " conf_threshold, iou_threshold) VALUES (?, 'Naawan', 1, 't', 0.25, 0.45)",
        ("2026-01-01T00:00:00+00:00",),
    )
    old.commit()
    old.close()

    database = Database(cfg)
    database.initialise()
    database.initialise()  # a second process starting finds it already there
    with database.read() as conn:
        assert conn.execute("SELECT camera FROM detection_event").fetchone()[0] == 0

    box = Detection(1, "aphid_damage", 0.8, x=0.1, y=0.1, w=0.2, h=0.2)
    event_id = database.insert_event(
        site="Naawan", camera=1, frame_seq=2, detections=[box], model_version="t",
        conf_threshold=0.25, iou_threshold=0.45, severity="low",
    )
    assert database.get_event(event_id)["camera"] == 1

