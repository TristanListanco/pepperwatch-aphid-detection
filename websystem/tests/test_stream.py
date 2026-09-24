"""Frame and event streaming: /frame.jpg, MJPEG, and SSE."""

from __future__ import annotations

import json

import pytest

from pepperwatch.frames import make_writer
from pepperwatch.render import encode_jpeg

import numpy as np

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def published_frame(app, cfg):
    """Publish one real JPEG into the app's frame transport."""
    writer = make_writer(cfg)
    image = np.full((480, 640, 3), 120, np.uint8)
    jpeg = encode_jpeg(image, 75)
    writer.publish(jpeg, seq=99, preview_fps=25.0, inference_fps=8.5)
    yield jpeg
    writer.close()


# -- single frame ----------------------------------------------------------


def test_frame_jpeg_reports_503_with_no_worker(client) -> None:
    """Honest: the dashboard should say the feed is down, not serve a blank."""
    assert client.get("/frame.jpg").status_code == 503


def test_frame_jpeg_serves_a_real_jpeg(client, published_frame) -> None:
    response = client.get("/frame.jpg")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    data = response.get_data()
    assert data[:2] == b"\xff\xd8"  # SOI
    assert data[-2:] == b"\xff\xd9"  # EOI
    assert data == published_frame


def test_frame_jpeg_exposes_pipeline_metadata(client, published_frame) -> None:
    headers = client.get("/frame.jpg").headers
    assert headers["X-Frame-Seq"] == "99"
    assert float(headers["X-Preview-Fps"]) == pytest.approx(25.0)
    assert float(headers["X-Inference-Fps"]) == pytest.approx(8.5)
    assert float(headers["X-Frame-Age"]) >= 0.0


def test_frame_jpeg_returns_304_for_an_unchanged_frame(client, published_frame) -> None:
    """Polling faster than the camera must not re-send identical bytes.

    Several phones polling one radio is the common case, so this is the
    difference between 0.4 Mbit/s and several times that.
    """
    etag = client.get("/frame.jpg").headers["ETag"]
    assert etag == '"99"'

    second = client.get("/frame.jpg", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.get_data() == b""


def test_frame_jpeg_is_not_cached_by_the_browser(client, published_frame) -> None:
    assert "no-store" in client.get("/frame.jpg").headers["Cache-Control"]


# -- MJPEG -----------------------------------------------------------------


def test_mjpeg_declares_a_multipart_boundary(client, published_frame) -> None:
    response = client.get("/stream.mjpg")
    assert response.status_code == 200
    assert response.mimetype == "multipart/x-mixed-replace"
    assert "boundary=pepperwatchframe" in response.headers["Content-Type"]
    response.close()


def test_mjpeg_first_part_parses_with_a_correct_content_length(client, published_frame) -> None:
    response = client.get("/stream.mjpg")
    stream = response.response
    chunk = next(iter(stream))
    response.close()

    header, _, payload = chunk.partition(b"\r\n\r\n")
    text = header.decode("ascii")
    assert "--pepperwatchframe" in text
    assert "Content-Type: image/jpeg" in text
    assert "X-Frame-Seq: 99" in text

    declared = int(
        [line for line in text.splitlines() if line.startswith("Content-Length:")][0].split(":")[1]
    )
    assert declared == len(published_frame)
    assert payload[: len(published_frame)] == published_frame


def test_mjpeg_viewer_count_is_capped(client, cfg, published_frame) -> None:
    """Bandwidth, not CPU, is the limit: ~6-7 Mbit/s per viewer over one radio."""
    limit = cfg.web.max_stream_clients
    open_streams = []
    try:
        for _ in range(limit):
            response = client.get("/stream.mjpg")
            next(iter(response.response))  # force the generator to register
            open_streams.append(response)

        refused = client.get("/stream.mjpg")
        assert refused.status_code == 503
        assert refused.get_json()["max"] == limit
        assert refused.headers["Retry-After"] == "5"
    finally:
        for response in open_streams:
            response.close()


def test_mjpeg_slot_is_released_when_a_viewer_disconnects(client, published_frame) -> None:
    before = client.get("/stream/status").get_json()["mjpeg_viewers"]

    response = client.get("/stream.mjpg")
    next(iter(response.response))
    during = client.get("/stream/status").get_json()["mjpeg_viewers"]
    response.close()

    after = client.get("/stream/status").get_json()["mjpeg_viewers"]
    assert during == before + 1
    assert after == before


# -- SSE -------------------------------------------------------------------


def sse_chunk(stream) -> str:
    """Next SSE frame as text.

    The hub yields str for readability; Werkzeug encodes it on the way out, so
    tests see bytes.
    """
    chunk = next(stream)
    return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk


def test_sse_opens_with_a_hello_snapshot(client) -> None:
    """A page loading between publications must still paint immediately."""
    response = client.get("/stream/events")
    assert response.mimetype == "text/event-stream"
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["X-Accel-Buffering"] == "no"

    chunk = sse_chunk(iter(response.response))
    response.close()

    assert chunk.startswith("event: hello")
    payload = json.loads(chunk.split("data: ", 1)[1])
    assert "worker" in payload and "latest" in payload


def test_sse_delivers_a_published_event(app, client) -> None:
    hub = app.config["PW_HUB"]
    response = client.get("/stream/events")
    stream = iter(response.response)
    sse_chunk(stream)  # consume hello

    hub.publish("telemetry/system", {"cpu_temp_c": 63.5, "synthetic": True})
    chunk = sse_chunk(stream)
    response.close()

    assert chunk.startswith("event: telemetry/system")
    assert json.loads(chunk.split("data: ", 1)[1])["cpu_temp_c"] == 63.5


def test_sse_client_count_is_reported(app, client) -> None:
    response = client.get("/stream/events")
    next(iter(response.response))
    assert app.config["PW_HUB"].client_count == 1
    response.close()


def test_sse_rejects_clients_past_the_limit(cfg) -> None:
    """Refusing beats accepting everyone and starving the lot."""
    from pepperwatch.web import create_app

    cfg.web._data["max_sse_clients"] = 2  # noqa: SLF001
    app = create_app(cfg, start_hub=False)
    client = app.test_client()

    open_streams = []
    try:
        for _ in range(2):
            response = client.get("/stream/events")
            next(iter(response.response))
            open_streams.append(response)

        refused = client.get("/stream/events")
        chunk = sse_chunk(iter(refused.response))
        refused.close()
        assert chunk.startswith("event: rejected")
        assert json.loads(chunk.split("data: ", 1)[1])["max"] == 2
    finally:
        for response in open_streams:
            response.close()


def test_a_slow_subscriber_drops_messages_instead_of_blocking(cfg) -> None:
    """The property that protects the worker: bounded queues, oldest discarded.

    Without this, one phone at the edge of Wi-Fi range could apply
    back-pressure all the way up to the inference loop.
    """
    from pepperwatch.web.hub import Subscriber
    import queue

    subscriber = Subscriber(queue=queue.Queue(maxsize=3))
    for index in range(10):
        subscriber.offer(f"message-{index}")

    assert subscriber.queue.qsize() == 3
    assert subscriber.dropped == 7
    # The newest survive; the oldest were discarded.
    remaining = [subscriber.queue.get_nowait() for _ in range(3)]
    assert remaining == ["message-7", "message-8", "message-9"]


def test_worker_state_treats_silence_as_offline(app) -> None:
    """A SIGKILLed worker announces nothing; the dashboard must not claim it is up."""
    hub = app.config["PW_HUB"]
    assert hub.worker_state()["alive"] is False

    hub._on_message("pepperwatch/status/worker", {"value": "online"})
    assert hub.worker_state()["alive"] is True

    # Rewind the last-seen clock past the staleness window.
    hub._worker_seen_at -= hub.worker_state()["stale_after_s"] + 1
    assert hub.worker_state()["alive"] is False
    assert hub.worker_state()["announced"] == "online"
