"""Per-session CSV records: the data the results chapter is written from."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest

from pepperwatch.session import (
    INFERENCE_COLUMNS,
    SUMMARY_COLUMNS,
    SYSTEM_COLUMNS,
    Reservoir,
    SessionRecorder,
    list_sessions,
    rebuild_index,
    request_next_label,
)

META = {
    "accelerator": {"kind": "hailo", "name": "Hailo-8L", "profile": "hailo", "reason": "test",
                    "hailo": {"arch": "hailo8l", "runtime_version": "4.20.0", "pcie_link": "8.0 GT/s PCIe x1"}},
    "camera": {"kind": "csi", "model": "imx708", "device": "0", "width": 640, "height": 480},
    "detector": {"kind": "hailo", "model_file": "pepperwatch_hailo8l.hef", "input_size": 640, "synthetic": False},
    "settings": {"preview_fps": 60, "conf_threshold": 0.25, "iou_threshold": 0.45},
    "host": {"board": "Raspberry Pi 5 Model B Rev 1.1", "ram_mb": 16000},
}


class Clock:
    """A controllable time.monotonic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


def _rows(path: Path) -> list[dict]:
    with path.open() as handle:
        return list(csv.DictReader(handle))


def test_a_session_writes_its_files_with_headers(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, META)
    recorder.close()

    folder = recorder.directory
    assert folder.name.endswith("_hailo")
    assert json.loads((folder / "session.json").read_text())["camera"]["model"] == "imx708"
    with (folder / "inference.csv").open() as handle:
        assert tuple(next(csv.reader(handle))) == INFERENCE_COLUMNS
    with (folder / "system.csv").open() as handle:
        assert tuple(next(csv.reader(handle))) == SYSTEM_COLUMNS
    assert json.loads((folder / "summary.json").read_text())["status"] == "complete"


def test_inference_rows_and_their_summary(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, META)
    for index in range(100):
        recorder.record_inference({
            "frame_seq": index, "preprocess_ms": 1.0, "infer_ms": 10.0 + index % 10,
            "postprocess_ms": 0.5, "total_ms": 11.5 + index % 10, "end_to_end_ms": 20.0,
            "detections": 2, "damage_boxes": 1 if index % 4 == 0 else 0, "severity": "low",
        })
    summary = recorder.close()

    rows = _rows(recorder.directory / "inference.csv")
    assert len(rows) == 100
    assert rows[0]["frame_seq"] == "0" and rows[0]["timestamp"]
    assert summary["inferences"] == 100
    assert summary["infer_ms_mean"] == pytest.approx(14.5)
    assert summary["infer_ms_p50"] == pytest.approx(14.5)
    assert 18.0 <= summary["infer_ms_p95"] <= 19.0
    assert summary["frames_with_damage"] == 25
    assert summary["detections_total"] == 200
    assert summary["accelerator"] == "Hailo-8L" and summary["hailo_arch"] == "hailo8l"


def test_energy_is_integrated_from_power_samples(tmp_path: Path, clock: Clock) -> None:
    recorder = SessionRecorder(tmp_path, META)
    for _ in range(11):  # 6 W for 10 seconds
        recorder.record_system({"power_w": 6.0, "power_source": "ina219", "inference_fps": 30.0,
                                "cpu_temp_c": 55.0})
        clock.now += 1.0
    summary = recorder.close()

    assert summary["energy_wh"] == pytest.approx(60.0 / 3600.0, rel=1e-3)
    assert summary["power_mean_w"] == pytest.approx(6.0)
    assert summary["power_source"] == "ina219"
    assert summary["fps_per_watt"] == pytest.approx(5.0)
    last = _rows(recorder.directory / "system.csv")[-1]
    assert float(last["energy_wh"]) == pytest.approx(60.0 / 3600.0, rel=1e-3)


def test_a_stall_does_not_book_hours_of_energy(tmp_path: Path, clock: Clock) -> None:
    recorder = SessionRecorder(tmp_path, META)
    recorder.record_system({"power_w": 5.0})
    clock.now += 3600.0
    recorder.record_system({"power_w": 5.0})
    assert recorder.close()["energy_wh"] == pytest.approx(5.0 * 5.0 / 3600.0, rel=1e-4)


def test_paused_samples_do_not_count_toward_mean_fps(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, META)
    recorder.record_system({"inference_fps": 20.0})
    recorder.record_system({"inference_fps": 0.0, "paused": True})
    assert recorder.close()["inference_fps_mean"] == pytest.approx(20.0)


def test_throttling_and_backoff_time_is_totalled(tmp_path: Path, clock: Clock) -> None:
    recorder = SessionRecorder(tmp_path, META)
    for throttled in (False, True, True, False):
        recorder.record_system({"throttled_now": throttled, "throttled_flags": "0x50000" if throttled else "0x0",
                                "thermal_level": 1 if throttled else 0})
        clock.now += 1.0
    summary = recorder.close()
    assert summary["throttled_s"] == pytest.approx(2.0)
    assert summary["thermal_backoff_s"] == pytest.approx(2.0)


def test_the_cross_session_index_lists_every_session(tmp_path: Path, clock: Clock) -> None:
    from datetime import datetime, timedelta, timezone

    first = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    SessionRecorder(tmp_path, META, now=first).close()
    cpu_meta = {**META, "accelerator": {"kind": "cpu", "name": "CPU", "profile": "cpu", "reason": "test"}}
    running = SessionRecorder(tmp_path, cpu_meta, now=first + timedelta(hours=1))

    rows = _rows(tmp_path / "sessions.csv")
    assert tuple(rows[0]) == SUMMARY_COLUMNS
    assert [r["accelerator"] for r in rows] == ["Hailo-8L", "CPU"]  # oldest first
    assert [r["status"] for r in rows] == ["complete", "running"]
    assert list_sessions(tmp_path)[0]["session_id"] == running.session_id  # newest first
    running.close()


def test_a_deleted_session_drops_out_of_the_index(tmp_path: Path) -> None:
    import shutil

    recorder = SessionRecorder(tmp_path, META)
    recorder.close()
    shutil.rmtree(recorder.directory)
    rebuild_index(tmp_path)
    assert _rows(tmp_path / "sessions.csv") == []


def test_the_next_session_takes_the_requested_label(tmp_path: Path) -> None:
    request_next_label(tmp_path, "Row 3, noon")
    labelled = SessionRecorder(tmp_path, META)
    unlabelled = SessionRecorder(tmp_path, META)
    assert labelled.label == "Row 3, noon" and labelled.session_id.endswith("_hailo_row-3-noon")
    assert unlabelled.label == ""
    labelled.close()
    unlabelled.close()


def test_rows_after_close_are_ignored(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path, META)
    recorder.close()
    recorder.record_inference({"frame_seq": 1})
    recorder.record_system({"power_w": 1.0})
    assert _rows(recorder.directory / "inference.csv") == []


def test_reservoir_percentiles_stay_close_on_a_long_stream() -> None:
    sample = Reservoir(size=2000)
    for value in range(100_000):
        sample.add(float(value))
    assert sample.percentile(0.5) == pytest.approx(50_000, rel=0.05)
    assert sample.percentile(0.95) == pytest.approx(95_000, rel=0.03)
    assert Reservoir().percentile(0.5) is None
