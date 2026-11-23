"""Flask application factory for ``pepperwatch-web``.

Everything shared lives in ``app.config`` under ``PW_*`` keys, built once at
startup: the config, the database, the frame reader, the MQTT-to-SSE hub, and
the advisory content. The hub in particular must be **one instance per
process**, because it holds the process's single MQTT subscription — which is
why the deploy unit runs gunicorn with exactly one worker and many threads.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, url_for

from .. import debuglog
from ..advisory import load_advisory
from ..config import Config, load
from ..db import Database
from ..frames import make_reader
from .hub import EventHub

log = logging.getLogger(__name__)

YEAR_S = 365 * 24 * 3600
COMPRESSIBLE = {"text/html", "text/css", "text/javascript", "application/javascript", "application/json", "image/svg+xml"}


def create_app(cfg: Config | None = None, *, start_hub: bool = True) -> Flask:
    cfg = cfg or load()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        PW_CONFIG=cfg,
        PW_STARTED_AT=time.time(),
        SECRET_KEY=cfg.web.secret_key,
        # Captured frames are immutable once written, so they can be cached hard
        # while templates and scripts stay fresh during development.
        SEND_FILE_MAX_AGE_DEFAULT=0,
        JSON_SORT_KEYS=False,
        # No request here needs more; a bigger body is refused before it can
        # use up a small board's memory.
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    database = Database(cfg)
    # The worker is the production writer; the REST ingest path exists for
    # §3.5.1's tests and for the alternative the manuscript describes in §3.4.3.
    database.initialise(writer="worker")
    app.config["PW_DB"] = database
    # Renamed in Settings: the saved name wins over the config file's.
    cfg.site._data["name"] = database.meta("site_name") or cfg.site.name  # noqa: SLF001
    # One reader per camera the worker may run; a camera that is not running
    # just has no frames yet.
    app.config["PW_FRAMES"] = [make_reader(cfg, i) for i in range(max(1, int(cfg.camera.max_cameras)))]
    app.config["PW_ADVISORY"] = load_advisory(cfg)
    app.config["PW_ADMIN_PIN"] = _admin_pin(cfg)
    try:  # a debug log started before this process (re)started
        debuglog.sync(cfg.debug_root, "web")
    except OSError:
        logging.getLogger(__name__).exception("could not open the debug log")
    # Taken before any change from Settings, which overwrites them in cfg.
    app.config["PW_THRESHOLD_DEFAULTS"] = {
        key: float(cfg.inference.get(key)) for key in ("conf_threshold", "iou_threshold")
    }

    hub = EventHub(cfg)
    app.config["PW_HUB"] = hub
    if start_hub:
        hub.start()

    from .routes_api import bp as api_bp
    from .routes_pages import bp as pages_bp
    from .routes_portal import bp as portal_bp
    from .routes_sessions import bp as sessions_bp
    from .routes_stream import bp as stream_bp

    # Portal first: its probe routes must win over the catch-all.
    app.register_blueprint(portal_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(stream_bp)
    app.register_blueprint(pages_bp)

    # Pages load the stylesheet and scripts from /static/v/<version>/, a
    # version taken from the files' contents, so a phone can keep them for a
    # year and still get new ones after an update. Module imports ("./words.js")
    # and the stylesheet's font are relative, so they inherit the version.
    version = _static_version(Path(app.static_folder))

    @app.get("/static/v/<build>/<path:filename>")
    def versioned_static(build: str, filename: str):
        current = build == version  # an older page may still ask for its build
        response = send_from_directory(app.static_folder, filename, max_age=YEAR_S if current else 0)
        response.cache_control.immutable = current
        return response

    app.jinja_env.globals["asset"] = lambda name: url_for("versioned_static", build=version, filename=name)

    @app.errorhandler(404)
    def not_found(error):
        """Send stray browser requests to the dashboard.

        This is half of what makes the captive portal work: a device probing an
        arbitrary URL, having had every hostname resolved to this Pi by the
        wildcard DNS, lands on the dashboard instead of an error. API paths are
        exempt — a client calling a mistyped endpoint needs a 404, not HTML.
        """
        if request.path.startswith(("/api/", "/stream", "/static/", "/media/")):
            return jsonify({"error": "not found", "path": request.path}), 404
        return redirect(url_for("pages.dashboard"), code=302)

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover — defensive
        log.exception("unhandled error on %s", request.path)
        return jsonify({"error": "internal server error"}), 500

    @app.after_request
    def security_headers(response):
        # No external hosts are reachable from a field Pi anyway; a strict CSP
        # makes that explicit and also proves the §3.3.2.2 "no CDN" claim at
        # runtime rather than only in the source.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.after_request
    def compress(response):
        # About 60 % less to send over the hotspot. Streams (live video, SSE,
        # the exports) and already-compressed images and fonts are left alone.
        if (
            response.status_code != 200
            or response.mimetype not in COMPRESSIBLE
            or "gzip" not in request.headers.get("Accept-Encoding", "")
            or "Content-Encoding" in response.headers
        ):
            return response
        response.direct_passthrough = False  # static files: read the body here
        body = response.get_data()
        if len(body) < 1024:
            return response
        response.set_data(gzip.compress(body, compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
        etag, weak = response.get_etag()
        if etag and not weak:
            response.set_etag(etag, weak=True)  # the bytes differ from the uncompressed file's
        return response

    log.info("web app ready: site=%s db=%s", cfg.site.name, database.path)
    return app


def _static_version(folder: Path) -> str:
    """A short hash of every static file, so any edit gives new URLs."""
    digest = hashlib.sha1(usedforsecurity=False)
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _admin_pin(cfg: Config) -> str | None:
    """The PIN that guards clearing data and changing thresholds, if one is set up."""
    path = Path(str(cfg.web.admin_pin_file))
    try:
        pin = path.read_text().strip()
    except OSError:
        log.warning("no admin PIN at %s: clearing data and thresholds are unprotected", path)
        return None
    return pin or None
