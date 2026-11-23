"""Zooming a camera: the preview, the detector and saved photos see the same crop."""

from __future__ import annotations

import types

import numpy as np

from pepperwatch import worker as w
from pepperwatch.render import digital_zoom


def test_digital_zoom_enlarges_the_middle_to_the_same_size() -> None:
    picture = np.zeros((480, 640, 3), np.uint8)
    picture[120:360, 160:480] = 255  # the middle half of each side, white; a black border
    zoomed = digital_zoom(picture, 2.0)
    assert zoomed.shape == picture.shape
    assert zoomed.min() == 255  # 2x: the middle half fills the frame, the border is gone
    assert digital_zoom(picture, 1.0) is picture


class _Camera:
    def __init__(self, sensor: bool) -> None:
        self.sensor, self.zooms = sensor, []

    def set_zoom(self, zoom: float) -> bool:
        self.zooms.append(zoom)
        return self.sensor


def _worker(sensor: bool) -> types.SimpleNamespace:
    saved: dict = {}
    db = types.SimpleNamespace(set_meta=lambda key, value: saved.__setitem__(key, value),
                               log_system=lambda **kwargs: None)
    return types.SimpleNamespace(cameras=[_Camera(sensor)], camera_zoom=[1.0], _sensor_zoom=[False],
                                 latest=[w.LatestFrame()], db=db, saved=saved)


def test_zoom_is_kept_and_the_sensor_crops_when_it_can() -> None:
    usb = _worker(sensor=False)
    w.Worker._zoom_camera(usb, {"camera": 0, "zoom": 2})
    assert usb.camera_zoom == [2.0] and usb._sensor_zoom == [False]  # the worker enlarges it
    assert usb.saved == {"camera_zoom_0": "2.0"}

    csi = _worker(sensor=True)
    w.Worker._zoom_camera(csi, {"camera": 0, "zoom": 3})
    assert csi._sensor_zoom == [True]
    w.Worker._zoom_camera(csi, {"camera": 0, "zoom": 1})  # back to the whole sensor
    assert csi.cameras[0].zooms == [3.0, 1.0] and csi._sensor_zoom == [False]


def test_a_zoom_not_offered_changes_nothing() -> None:
    worker = _worker(sensor=False)
    for payload in ({"camera": 0, "zoom": 2.5}, {"camera": 0, "zoom": True}, {"camera": 0, "zoom": "2"},
                    {"camera": 1, "zoom": 2}):
        w.Worker._zoom_camera(worker, payload)
    assert worker.camera_zoom == [1.0] and worker.saved == {}


def test_the_dashboard_forwards_a_zoom(app, client, monkeypatch) -> None:
    import pepperwatch.web.routes_api as api

    sent: list = []
    monkeypatch.setattr(api, "_publish_command", lambda topic, payload: sent.append((topic, payload)) or True)
    app.config["PW_HUB"]._on_message("pepperwatch/status/session", {"cameras": [{"index": 0}]})
    assert client.post("/api/camera/0", json={"zoom": 4}).status_code == 400
    assert client.post("/api/camera/1", json={"zoom": 2}).status_code == 404
    assert client.post("/api/camera/0", json={"zoom": 1.5}).status_code == 202
    assert sent == [("pepperwatch/cmd/camera", {"camera": 0, "zoom": 1.5})]
