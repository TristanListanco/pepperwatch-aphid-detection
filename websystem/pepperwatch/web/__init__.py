"""Flask application factory for ``pepperwatch-web``.

Everything shared lives in ``app.config`` under ``PW_*`` keys, built once at
startup: the config, the database, the frame reader, the MQTT-to-SSE hub, and
the advisory content. The hub in particular must be **one instance per
process**, because it holds the process's single MQTT subscription — which is
why the deploy unit runs gunicorn with exactly one worker and many threads.
"""

from __future__ import annotations

import logging
import time

from flask import Flask, jsonify, redirect, request, url_for

from ..advisory import load_advisory
from ..config import Config, load
from ..db import Database
from ..frames import make_reader
from .hub import EventHub

log = logging.getLogger(__name__)


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
    app.config["PW_FRAMES"] = make_reader(cfg)
    app.config["PW_ADVISORY"] = load_advisory(cfg)

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

    log.info("web app ready: site=%s db=%s", cfg.site.name, database.path)
    return app
