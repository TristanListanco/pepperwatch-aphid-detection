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


# -- control ---------------------------------------------------------------


def test_capture_reports_503_when_the_worker_is_unreachable(client) -> None:
    """The bus is down in tests, so this is the honest answer."""
    response = client.post("/api/capture", json={})
    assert response.status_code == 503
    assert "worker unreachable" in response.get_json()["error"]


def test_inference_control_validates_the_action(client) -> None:
    assert client.post("/api/inference/pause").status_code == 503  # valid, bus down
    assert client.post("/api/inference/explode").status_code == 400


def test_config_exposes_only_the_tunables(client) -> None:
    body = client.get("/api/config").get_json()
    assert set(body) == {"site", "inference", "severity", "web", "locales", "advisory"}
    # Secrets must never appear.
    assert "secret_key" not in body["web"]


def test_config_default_threshold_is_not_figure_3_3s_value(client) -> None:
    """Documents the deliberate departure from Figure 3.3's 'Confidence Filter (80)'."""
    assert client.get("/api/config").get_json()["inference"]["conf_threshold"] == 0.25


def test_patch_config_updates_thresholds(client) -> None:
    response = client.patch("/api/config", json={"conf_threshold": 0.4})
    assert response.status_code == 200
    assert response.get_json()["updated"]["conf_threshold"] == 0.4
    assert client.get("/api/config").get_json()["inference"]["conf_threshold"] == 0.4


@pytest.mark.parametrize("value", [0.0, 1.0, -0.5, 1.5, "high"])
def test_patch_config_rejects_out_of_range_thresholds(client, value) -> None:
    assert client.patch("/api/config", json={"conf_threshold": value}).status_code == 400


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
