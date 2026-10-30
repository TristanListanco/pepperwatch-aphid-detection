"""Session records: list them, download their CSV, start a new one.

The worker writes the files (see ``pepperwatch.session``); this blueprint only
reads them. ``sessions.csv`` is built per request from the summaries on disk,
so it includes the running session's latest figures without the web process
ever writing into the worker's folders.
"""

from __future__ import annotations

import csv
import io
import re

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_from_directory

from ..mqtt_bus import Topics
from ..session import SUMMARY_COLUMNS, _format, list_sessions
from .routes_api import _publish_command

bp = Blueprint("sessions", __name__, url_prefix="/api")

DOWNLOADABLE = ("inference.csv", "system.csv", "summary.json", "session.json")
_SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_\-]{0,120}$")


def _root():
    return current_app.config["PW_CONFIG"].sessions_root


def _hub():
    return current_app.config["PW_HUB"]


@bp.get("/session")
def current_session():
    """The running session: its metadata and latest summary."""
    snapshot = _hub().snapshot()
    return jsonify(
        {
            "session": snapshot.get("session") or None,
            "summary": snapshot["latest"].get("session/summary"),
        }
    )


@bp.post("/session/new")
def new_session():
    """End this session and start another, re-detecting camera and AI HAT+."""
    payload = request.get_json(silent=True) or {}
    label = str(payload.get("label") or "").strip()[:80]
    if not _publish_command(Topics.CMD_SESSION, {"action": "new", "label": label}):
        return jsonify({"error": "worker unreachable — is pepperwatch-worker running?"}), 503
    return jsonify({"accepted": True, "label": label}), 202


@bp.get("/sessions")
def sessions():
    """Every recorded session's summary, newest first."""
    current = (_hub().session or {}).get("session_id")
    items = list_sessions(_root())
    for item in items:
        item["current"] = item.get("session_id") == current
        item["files"] = [
            name for name in DOWNLOADABLE if (_root() / item["session_id"] / name).exists()
        ]
    return jsonify({"sessions": items, "current": current})


@bp.get("/sessions.csv")
def sessions_csv():
    """One row per session: the table to start a results chapter from."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, SUMMARY_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in reversed(list_sessions(_root())):
        writer.writerow(_format(row))
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pepperwatch_sessions.csv"},
    )


@bp.get("/sessions/<session_id>/<name>")
def session_file(session_id: str, name: str):
    """Download one of a session's files."""
    if name not in DOWNLOADABLE or not _SESSION_ID.match(session_id):
        abort(404)
    directory = _root() / session_id
    if not (directory / name).is_file():
        abort(404)
    return send_from_directory(
        directory,
        name,
        as_attachment=True,
        download_name=f"{session_id}_{name}",
        max_age=0,
    )
