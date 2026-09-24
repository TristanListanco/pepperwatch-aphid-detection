"""Run the development server: ``python -m pepperwatch.web``.

Use gunicorn in production — see ``wsgi.py`` and the deploy units. Flask's
server is single-threaded by default, which would let one MJPEG viewer block
every other request, so threading is forced on here.
"""

from __future__ import annotations

import argparse
import logging

from ..config import load
from . import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load()
    app = create_app(cfg)
    app.run(
        host=args.host or cfg.web.host,
        port=args.port or cfg.web.port,
        debug=args.debug,
        threaded=True,
        use_reloader=False,  # the reloader would duplicate the MQTT subscription
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
