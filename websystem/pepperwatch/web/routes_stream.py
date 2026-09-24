"""Streaming endpoints: the live frame, MJPEG, and SSE.

Bandwidth is the constraint that shapes this file. A 640x480 JPEG at quality 75
is roughly 40-55 KB, so MJPEG at 17 fps is about 6-7 Mbit/s per viewer. The Pi
5's onboard radio in AP mode realistically delivers 10-20 Mbit/s on 2.4 GHz in
an open field with interference — two or three viewers, not ten.

So the dashboard does **not** open an MJPEG stream by default. It polls
``/frame.jpg`` about once a second, which costs around 0.4 Mbit/s, and only
upgrades to MJPEG when the user taps to go live. Figure 3.4 already draws this:
the wireframe shows an inactive feed with a "Start Camera" button and a "Live"
toggle, so the cheap default is also the faithful one.

Streams never hold a database transaction. A read transaction left open for the
life of an MJPEG response would block WAL checkpointing for as long as the phone
stayed connected, and the ``-wal`` file would grow without bound.
"""

from __future__ import annotations

import logging
import threading
import time

from flask import Blueprint, Response, current_app, jsonify, request

log = logging.getLogger(__name__)

bp = Blueprint("stream", __name__)

_BOUNDARY = "pepperwatchframe"

# Concurrent MJPEG viewers, counted across the process. One process is
# guaranteed by the deploy units, so a module-level counter is accurate.
_viewers = 0
_viewers_lock = threading.Lock()


def _cfg():
    return current_app.config["PW_CONFIG"]


def _reader():
    return current_app.config["PW_FRAMES"]


@bp.get("/frame.jpg")
def frame_jpeg():
    """The current frame as a single JPEG.

    The polled default. ETag is the frame sequence, so a client that polls
    faster than the camera produces gets a cheap 304 instead of the same bytes
    again — which matters when several phones poll at once over one radio.
    """
    packet = _reader().read()
    if packet is None:
        return jsonify({"error": "no frame available — is the worker running?"}), 503

    etag = f'"{packet.seq}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})

    return Response(
        packet.jpeg,
        mimetype="image/jpeg",
        headers={
            "ETag": etag,
            "Cache-Control": "no-store, must-revalidate",
            "X-Frame-Seq": str(packet.seq),
            "X-Frame-Age": f"{packet.age_s:.3f}",
            "X-Preview-Fps": f"{packet.preview_fps:.2f}",
            "X-Inference-Fps": f"{packet.inference_fps:.2f}",
        },
    )


@bp.get("/stream.mjpg")
def stream_mjpeg():
    """Multipart MJPEG, rate-limited and capped by viewer count."""
    global _viewers
    cfg = _cfg()
    limit = int(cfg.web.max_stream_clients)

    with _viewers_lock:
        if _viewers >= limit:
            # Refusing plainly beats degrading everyone's stream silently.
            return (
                jsonify(
                    {
                        "error": "too many live viewers",
                        "max": limit,
                        "hint": "close a live view, or poll /frame.jpg instead",
                    }
                ),
                503,
                {"Retry-After": "5"},
            )
        _viewers += 1

    target_fps = max(1.0, float(cfg.web.stream_fps))
    interval = 1.0 / target_fps
    reader = _reader()

    def generate():
        global _viewers
        last_seq = -1
        try:
            while True:
                began = time.monotonic()
                packet = reader.read()

                # Only send genuinely new frames: repeating one wastes the
                # radio, which is the scarce resource here.
                if packet is not None and packet.seq != last_seq:
                    last_seq = packet.seq
                    header = (
                        f"--{_BOUNDARY}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(packet.jpeg)}\r\n"
                        f"X-Frame-Seq: {packet.seq}\r\n\r\n"
                    ).encode("ascii")
                    yield header + packet.jpeg + b"\r\n"

                elapsed = time.monotonic() - began
                if elapsed < interval:
                    time.sleep(interval - elapsed)
        except GeneratorExit:
            raise
        finally:
            with _viewers_lock:
                _viewers = max(0, _viewers - 1)

    return Response(
        generate(),
        mimetype=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.get("/stream/events")
def stream_events():
    """Server-Sent Events: the MQTT bus, bridged to the browser.

    ``X-Accel-Buffering: no`` and the disabled implicit buffering matter — any
    proxy or buffer between here and the phone would otherwise hold events until
    a block filled, which for a 200-byte telemetry message could be minutes.
    """
    return Response(
        current_app.config["PW_HUB"].stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@bp.get("/stream/status")
def stream_status():
    """Viewer and subscriber counts, so the UI can show the slots in use."""
    with _viewers_lock:
        viewers = _viewers
    cfg = _cfg()
    return jsonify(
        {
            "mjpeg_viewers": viewers,
            "mjpeg_limit": cfg.web.max_stream_clients,
            "sse_clients": current_app.config["PW_HUB"].client_count,
            "sse_limit": cfg.web.max_sse_clients,
            "stream_fps": cfg.web.stream_fps,
        }
    )
