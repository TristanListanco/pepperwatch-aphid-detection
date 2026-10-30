"""The REST API.

Manuscript §3.3.2.1 assigns HTTP REST the "larger, high-density payloads" —
images and historical data — while MQTT carries telemetry. This blueprint is
that half, plus the control endpoints the dashboard's buttons call.

Mutating endpoints are gated behind a shared PIN when one is configured. The
manuscript does not mention authentication, and on an open field access point
that is a real gap: anyone within Wi-Fi range could otherwise pause the detector
or rewrite thresholds mid-trial. The PIN is deliberately minimal — it is not
meant to resist an attacker, only to stop a curious passer-by from silently
invalidating a data-collection run.
"""

from __future__ import annotations

import csv
import hmac
import io
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, current_app, jsonify, request

from ..models import Detection
from ..session import csv_safe
from ..mqtt_bus import Topics

log = logging.getLogger(__name__)

bp = Blueprint("api", __name__, url_prefix="/api")

# CSV export column order. Fixed rather than derived from the row dict so the
# exported files stay comparable across runs and schema additions.
EXPORT_COLUMNS = (
    "event_id", "captured_at", "site", "severity", "detection_count",
    "max_confidence", "damaged_area", "class_name", "confidence",
    "x", "y", "w", "h", "model_version", "conf_threshold", "iou_threshold",
    "inference_ms", "preview_fps", "inference_fps", "trigger", "synthetic",
    "image_path",
)


def _cfg():
    return current_app.config["PW_CONFIG"]


def _db():
    return current_app.config["PW_DB"]


def _hub():
    return current_app.config["PW_HUB"]


def _advisory():
    return current_app.config["PW_ADVISORY"]


def _locale() -> str:
    """Requested locale, restricted to what the content file actually has."""
    requested = request.args.get("locale", "en")
    return requested if requested in _advisory().locales else "en"


# A 6-digit PIN falls to a script in minutes without a limit: five wrong
# guesses from one address lock it out for five minutes.
_PIN_MAX_FAILURES = 5
_PIN_LOCKOUT_S = 300.0
_pin_lock = threading.Lock()


def require_pin(view):
    """Gate a mutating endpoint behind the configured PIN, if any."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        expected = str(_cfg().web.control_pin or "")
        if not expected:
            return view(*args, **kwargs)

        client = request.remote_addr or "unknown"
        now = time.monotonic()
        failures = current_app.config.setdefault("PW_PIN_FAILURES", {})
        with _pin_lock:
            recent = [t for t in failures.get(client, []) if now - t < _PIN_LOCKOUT_S]
            failures[client] = recent
            if len(recent) >= _PIN_MAX_FAILURES:
                return jsonify({"error": "too many wrong PINs; try again in five minutes"}), 429

        body = request.get_json(silent=True)
        supplied = request.headers.get("X-Pepper-Pin") or (
            body.get("pin", "") if isinstance(body, dict) else ""
        )
        if not supplied:
            # Not a guess: a phone that has no PIN yet. Not counted, so it
            # cannot lock its owner out before they have typed one.
            return jsonify({"error": "this needs the control PIN: enter it in Settings"}), 403
        if not hmac.compare_digest(str(supplied).encode(), expected.encode()):
            with _pin_lock:
                failures.setdefault(client, []).append(now)
            return jsonify({"error": "wrong control PIN: check it in Settings"}), 403
        return view(*args, **kwargs)

    return wrapper


def _publish_command(topic: str, payload: dict) -> bool:
    """Send a command to the worker, reporting whether the bus accepted it."""
    bus = _hub().bus
    if bus is None or not bus.connected:
        return False
    return bus.publish(topic, payload, qos=1)


# -- status ----------------------------------------------------------------


@bp.get("/health")
def health():
    """Liveness and provenance for the whole system."""
    from ..config import detect_board_info, total_memory_mb

    cfg = _cfg()
    board = detect_board_info()
    return jsonify(
        {
            "status": "ok",
            "site": cfg.site.name,
            "board": {
                "model": board.model,
                "family": board.family,
                "key": board.key,
                "cpu": board.cpu,
                "tier": board.tier,
                "profiles": cfg.board_profile,
                "supported": board.supported,
            },
            "total_memory_mb": round(total_memory_mb() or 0.0),
            "uptime_s": round(time.time() - current_app.config["PW_STARTED_AT"], 1),
            "worker": _hub().worker_state(),
            "mqtt_connected": _hub().connected,
            "sse_clients": _hub().client_count,
            "database": _db().stats(),
            "detector": _hub().detector,
            "session": _hub().session or None,
            "advisory_validated": _advisory().validated,
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )


@bp.get("/telemetry")
def telemetry():
    """Latest hardware health, live from MQTT with a database fallback.

    The live value is preferred because it is current; the stored one keeps the
    health panel populated when the worker is down, which is exactly when
    someone is most likely to be looking at it.
    """
    live = _hub().snapshot()["latest"].get("telemetry/system")
    if live:
        return jsonify({"source": "mqtt", **live})

    stored = _db().latest_telemetry()
    if stored:
        return jsonify({"source": "database", **stored})
    return jsonify({"source": "none"}), 204


@bp.get("/detections/latest")
def latest_detections():
    """The newest detection set, for the live overlay."""
    payload = _hub().snapshot()["latest"].get("detect/frame")
    if not payload:
        return jsonify({"frame_seq": None, "boxes": []}), 200

    severity = payload.get("severity", "none")
    return jsonify(
        {
            **payload,
            "advice": _advisory().for_severity(severity, locale=_locale()),
        }
    )


@bp.get("/pipeline")
def pipeline():
    """Frame-rate and latency counters, for the FPS chip."""
    payload = _hub().snapshot()["latest"].get("telemetry/pipeline")
    return jsonify(payload or {"preview_fps": 0.0, "inference_fps": 0.0})


# -- history ---------------------------------------------------------------


@bp.get("/events")
def list_events():
    """The Historical Detection Log, filtered and paginated."""
    args = request.args

    def maybe_float(name):
        raw = args.get(name)
        try:
            return float(raw) if raw not in (None, "") else None
        except ValueError:
            return None

    archived_arg = args.get("archived", "false").lower()
    archived = None if archived_arg == "all" else archived_arg in ("1", "true", "yes")

    try:
        limit = int(args.get("limit", 24))
        offset = int(args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    page = _db().list_events(
        since=args.get("from") or None,
        until=args.get("to") or None,
        class_name=args.get("class") or None,
        severity=args.get("severity") or None,
        min_confidence=maybe_float("min_confidence"),
        archived=archived,
        query=args.get("q") or None,
        limit=limit,
        offset=offset,
    )
    return jsonify(page)


@bp.get("/events/<int:event_id>")
def get_event(event_id: int):
    event = _db().get_event(event_id)
    if event is None:
        return jsonify({"error": f"no event {event_id}"}), 404
    event["advice"] = _advisory().for_severity(event["severity"], locale=_locale())
    return jsonify(event)


@bp.post("/events/<int:event_id>/archive")
@require_pin
def archive_event(event_id: int):
    payload = request.get_json(silent=True) or {}
    archived = bool(payload.get("archived", True))
    if not _db().set_archived(event_id, archived):
        return jsonify({"error": f"no event {event_id}"}), 404
    return jsonify({"event_id": event_id, "archived": archived})


@bp.get("/stats/weekly")
def weekly_stats():
    """Everything the four KPI tiles, the donut, and the banner need."""
    try:
        days = max(1, min(int(request.args.get("days", 7)), 365))
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400

    cfg = _cfg()
    stats = _db().weekly_stats(days=days, site=request.args.get("site") or None)
    stats.setdefault("site", cfg.site.name)
    if stats["site"] is None:
        stats["site"] = cfg.site.name

    locale = _locale()
    stats["insight"] = _advisory().insight(stats, locale=locale)
    stats["advice"] = _advisory().for_severity(stats["severity"], locale=locale)
    return jsonify(stats)


@bp.get("/recommendations")
@bp.get("/recommendations/<severity>")
def recommendations(severity: str = "none"):
    """Advisory text for a severity band."""
    return jsonify(_advisory().for_severity(severity, locale=_locale()))


# -- export ----------------------------------------------------------------


@bp.get("/export.csv")
def export_csv():
    """Stream the joined event and box rows as CSV.

    Streamed rather than assembled: exporting months of history should not spike
    memory on a device with other work to do. Table 3.4's Capture & Export
    Controls call for exactly this.
    """
    since = request.args.get("from") or None
    until = request.args.get("to") or None
    database = _db()

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(EXPORT_COLUMNS)
        yield buffer.getvalue()

        for row in database.iter_export_rows(since=since, until=until):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([csv_safe(row[column]) if column in row.keys() else "" for column in EXPORT_COLUMNS])
            yield buffer.getvalue()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="pepperwatch_{stamp}.csv"'},
    )


# -- control ---------------------------------------------------------------


@bp.post("/capture")
@require_pin
def capture():
    """Ask the worker for a high-resolution snapshot."""
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get("request_id") or uuid.uuid4())[:16]

    accepted = _publish_command(
        Topics.CMD_CAPTURE, {"request_id": request_id, "hi_res": bool(payload.get("hi_res", True))}
    )
    if not accepted:
        return jsonify({"error": "worker unreachable — is pepperwatch-worker running?"}), 503

    # 202: the worker performs this asynchronously, and the event appears in the
    # gallery when it lands. Returning 200 would imply the image already exists.
    return jsonify({"accepted": True, "request_id": request_id}), 202


@bp.post("/inference/<action>")
@require_pin
def inference_control(action: str):
    """Pause or resume inference, per Table 3.4's Capture & Export Controls."""
    if action not in ("pause", "resume"):
        return jsonify({"error": "action must be pause or resume"}), 400
    if not _publish_command(Topics.CMD_INFERENCE, {"action": action}):
        return jsonify({"error": "worker unreachable"}), 503
    return jsonify({"accepted": True, "action": action}), 202


@bp.get("/config")
def get_config():
    """The tunables the Settings page exposes. Never the whole config."""
    cfg = _cfg()
    return jsonify(
        {
            "site": {"name": cfg.site.name, "operator": cfg.site.operator, "role": cfg.site.role},
            "inference": {
                "conf_threshold": cfg.inference.conf_threshold,
                "iou_threshold": cfg.inference.iou_threshold,
                "input_size": cfg.inference.input_size,
                "target_fps": cfg.inference.target_fps,
                "class_names": list(cfg.inference.class_names),
            },
            "severity": cfg.severity.as_dict(),
            "web": {
                "stream_fps": cfg.web.stream_fps,
                "stream_width": cfg.web.stream_width,
                "max_stream_clients": cfg.web.max_stream_clients,
                "pin_required": bool(cfg.web.control_pin),
            },
            "locales": _advisory().locales,
            "advisory": _advisory().meta,
        }
    )


@bp.patch("/config")
def patch_config():
    """Adjust detection thresholds live.

    Applied in this process and forwarded to the worker, which owns the
    detector. Bounded to 0.01..0.99: a threshold of 0 or 1 makes the detector
    useless in opposite directions, and there is no reason to allow it.
    """
    payload = request.get_json(silent=True) or {}
    updates: dict[str, float] = {}

    for key in ("conf_threshold", "iou_threshold"):
        if key not in payload:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be a number"}), 400
        if not 0.01 <= value <= 0.99:
            return jsonify({"error": f"{key} must be between 0.01 and 0.99"}), 400
        updates[key] = value

    if not updates:
        return jsonify({"error": "nothing to update"}), 400

    cfg = _cfg()
    for key, value in updates.items():
        cfg.inference._data[key] = value  # noqa: SLF001

    forwarded = _publish_command(Topics.CMD_CONFIG, updates)
    return jsonify({"updated": updates, "forwarded_to_worker": forwarded})


@bp.post("/time")
@require_pin
def set_time():
    """Accept a clock reading from a connecting client.

    A gap the manuscript does not address: with no internet there is no NTP, and
    a Pi whose RTC battery is absent or flat boots to a meaningless date — which
    would make every timestamp in the Historical Detection Log worthless and
    collapse Table 3.4's historical-tracking purpose.

    Fitting the RTC battery is the real fix. This is the backstop, and it is
    deliberately narrow: forward jumps only, so a client with a bad clock cannot
    rewrite history backwards over existing rows, and every acceptance is
    audited into ``system_log``. It reports what it would do rather than setting
    the clock itself, because doing so needs root and belongs in a helper the
    deploy script installs.
    """
    payload = request.get_json(silent=True) or {}
    raw = payload.get("iso") or payload.get("epoch")
    if raw is None:
        return jsonify({"error": "provide iso or epoch"}), 400

    try:
        proposed = (
            datetime.fromtimestamp(float(raw), tz=timezone.utc)
            if isinstance(raw, (int, float))
            else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return jsonify({"error": "unparseable time"}), 400

    if proposed.tzinfo is None:
        proposed = proposed.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    drift_s = (proposed - now).total_seconds()

    if drift_s <= 0:
        return jsonify(
            {"accepted": False, "reason": "not a forward jump", "drift_s": round(drift_s, 1)}
        )
    if drift_s < 60:
        return jsonify({"accepted": False, "reason": "within tolerance", "drift_s": round(drift_s, 1)})

    _db().log_system(
        level="warn",
        event="time_sync_offered",
        source="web",
        detail=f"client proposed {proposed.isoformat()} (+{drift_s:.0f}s from {now.isoformat()})",
    )
    return jsonify(
        {
            "accepted": True,
            "applied": False,
            "reason": "recorded; applying the clock needs the privileged helper",
            "proposed": proposed.isoformat(timespec="seconds"),
            "drift_s": round(drift_s, 1),
            "hint": "fit the Pi 5 RTC battery (J5) to remove the need for this",
        }
    )


@bp.post("/data/clear")
@require_pin
def clear_detections():
    """Delete every saved detection, its boxes and its images.

    Requires ``{"confirm": "DELETE"}`` so a stray request cannot wipe the
    record. Session CSV files are left alone: they are measurements, not
    detections.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": 'send {"confirm": "DELETE"} to clear all detections'}), 400

    events = _db().clear_detections()
    root = _cfg().media_root.resolve()
    files = 0
    if root.is_dir():
        for path in sorted(root.rglob("*"), reverse=True):  # files before their folders
            if path.is_file() or path.is_symlink():
                path.unlink()
                files += 1
            elif path.is_dir():
                path.rmdir()
    _db().log_system(level="warn", event="detections_cleared", source="web",
                     detail=f"{events} events, {files} files")
    return jsonify({"deleted_events": events, "deleted_files": files})


# -- ingest ----------------------------------------------------------------


@bp.post("/detections")
@require_pin
def ingest_detections():
    """Accept a detection payload over HTTP and persist it.

    §3.3 says the inference layer writes to the database and §3.4.3 says the
    backend does. The worker takes the direct path by default, but this endpoint
    exists so the REST path is real and testable — and §3.5.1 specifically calls
    for sending "mock JSON payloads to the API endpoints" and asserting the
    bounding boxes land in SQLite. This is the endpoint that test drives.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object"}), 400

    try:
        frame_seq = int(payload["frame_seq"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "frame_seq is required and must be an integer"}), 400

    raw_boxes = payload.get("boxes", [])
    if not isinstance(raw_boxes, list):
        return jsonify({"error": "boxes must be a list"}), 400

    detections: list[Detection] = []
    for index, box in enumerate(raw_boxes):
        if not isinstance(box, dict):
            return jsonify({"error": f"boxes[{index}] must be an object"}), 400
        try:
            detection = Detection.from_dict(box)
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": f"boxes[{index}] is invalid: {exc}"}), 400

        # Class names reach the dashboard's charts; accept only the known ones.
        if detection.class_name not in _cfg().inference.class_names:
            return jsonify({"error": f"boxes[{index}].class_name must be one of "
                                     f"{list(_cfg().inference.class_names)}"}), 400
        if not 0.0 <= detection.confidence <= 1.0:
            return jsonify({"error": f"boxes[{index}].confidence must be within 0..1"}), 400
        if detection.w <= 0 or detection.h <= 0:
            return jsonify({"error": f"boxes[{index}] must have positive width and height"}), 400
        detections.append(detection)

    cfg = _cfg()
    severity = payload.get("severity")
    if severity is None:
        from ..detect import severity_from_config

        severity, _ = severity_from_config(detections, cfg)
    if severity not in ("none", "low", "moderate", "high"):
        return jsonify({"error": f"unknown severity {severity!r}"}), 400

    event_id = _db().insert_event(
        site=str(payload.get("site") or cfg.site.name),
        frame_seq=frame_seq,
        detections=detections,
        model_version=str(payload.get("model_version") or "rest-ingest"),
        conf_threshold=float(payload.get("conf_threshold", cfg.inference.conf_threshold)),
        iou_threshold=float(payload.get("iou_threshold", cfg.inference.iou_threshold)),
        severity=severity,
        trigger="auto",
        captured_at=payload.get("captured_at"),
        image_path=payload.get("image_path"),
        thumb_path=payload.get("thumb_path"),
        inference_ms=payload.get("inference_ms"),
        synthetic=bool(payload.get("synthetic", False)),
        note=payload.get("note"),
    )

    return (
        jsonify({"event_id": event_id, "detection_count": len(detections), "severity": severity}),
        201,
    )
