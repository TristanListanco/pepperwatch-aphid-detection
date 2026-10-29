"""Page routes. Jinja templates, no build step, per manuscript §3.3.2.2."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, send_from_directory

bp = Blueprint("pages", __name__)


def _context() -> dict:
    cfg = current_app.config["PW_CONFIG"]
    advisory = current_app.config["PW_ADVISORY"]
    return {
        "site_name": cfg.site.name,
        "operator": cfg.site.operator,
        "role": cfg.site.role,
        "locales": advisory.locales,
        "advisory_validated": advisory.validated,
        "class_names": list(cfg.inference.class_names),
        "stream_fps": cfg.web.stream_fps,
        "pin_required": bool(cfg.web.control_pin),
    }


@bp.get("/")
def dashboard():
    return render_template("dashboard.html", page="dashboard", **_context())


@bp.get("/archived")
def archived():
    return render_template("archived.html", page="archived", **_context())


@bp.get("/settings")
def settings():
    return render_template("settings.html", page="settings", **_context())


@bp.get("/media/<path:relative>")
def media(relative: str):
    """Serve a captured image from the media root.

    ``send_from_directory`` resolves and confines the path, so a traversal
    attempt escapes nothing. Checked explicitly as well, because this is the one
    route that maps user input onto the filesystem.
    """
    root: Path = current_app.config["PW_CONFIG"].media_root
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        abort(404)
    return send_from_directory(root, relative, max_age=3600)
