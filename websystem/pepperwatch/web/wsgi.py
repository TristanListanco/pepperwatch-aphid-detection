"""WSGI entry point.

    gunicorn --workers 1 --threads 16 --worker-class gthread \
             --bind 0.0.0.0:8000 pepperwatch.web.wsgi:app

``--workers 1`` is not a tuning choice but a correctness requirement: the
process holds one MQTT subscription in its EventHub, and a second worker would
mean a second subscription with browsers seeing whichever half of the traffic
their process received.
"""

from __future__ import annotations

import logging

from . import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

app = create_app()
