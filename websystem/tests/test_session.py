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
    recover_interrupted,
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


def test_ai_hat_figures_are_recorded_and_summarised(tmp_path: Path) -> None:
    """Busy share and chip temperature, each second and over the session. Paused seconds
    are idle by choice, so they don't pull the busy mean down."""
    recorder = SessionRecorder(tmp_path, META)
    recorder.record_system({"hailo_temp_c": 50.0, "hailo_busy_pct": 30.0, "hailo_infer_ms": 9.5})
    recorder.record_system({"hailo_temp_c": 54.0, "hailo_busy_pct": 50.0, "hailo_infer_ms": 9.9})
    recorder.record_system({"hailo_temp_c": 52.0, "hailo_busy_pct": 0.0, "paused": True})
    summary = recorder.close()
    assert summary["hailo_busy_pct_mean"] == pytest.approx(40.0)
    assert (summary["hailo_temp_mean_c"], summary["hailo_temp_max_c"]) == (52.0, 54.0)
    first = _rows(recorder.directory / "system.csv")[0]
    assert (first["hailo_busy_pct"], first["hailo_infer_ms"]) == ("30.0", "9.5")


CPU_META = {**META, "accelerator": {"kind": "cpu", "name": "CPU", "profile": "cpu", "reason": "test"},
            "detector": {"kind": "onnx", "model_file": "pepperwatch.onnx", "input_size": 640, "synthetic": False}}


def test_fps_per_watt_counts_only_the_seconds_spent_inferring(tmp_path: Path) -> None:
    """Paused seconds draw idle power and infer nothing. Counted in the power but
    not the fps, they made a session look more efficient than it was."""
    recorder = SessionRecorder(tmp_path, CPU_META)
    for _ in range(5):
        recorder.record_system({"power_w": 6.0, "power_source": "ina219", "inference_fps": 2.0})
    for _ in range(5):
        recorder.record_system({"power_w": 3.0, "power_source": "ina219", "inference_fps": 0.0, "paused": True})
    summary = recorder.close()
    assert summary["fps_per_watt"] == 0.333  # 2.0 / 6.0; was 2.0 / 4.5
    assert summary["power_mean_w"] == pytest.approx(4.5)  # the session's power is still all of it


@pytest.mark.parametrize(("meta", "sources", "expected"), [
    (META, ["pmic"], None),                      # the PMIC can't see the HAT
    (META, ["ina219", "pmic"], None),            # nor for the seconds the INA219 dropped out
    (META, ["ina219"], 5.0),
    (CPU_META, ["pmic"], 5.0),                   # board only, but nothing is left out between modes
])
def test_fps_per_watt_needs_the_hats_power_measured(tmp_path: Path, meta, sources, expected) -> None:
    recorder = SessionRecorder(tmp_path, meta)
    for source in sources:
        recorder.record_system({"power_w": 5.0, "power_source": source, "inference_fps": 25.0})
    summary = recorder.close()
    assert summary["fps_per_watt"] == expected
    assert summary["power_mean_w"] == 5.0  # the figure itself stays, labelled by power_source


@pytest.mark.parametrize(("flags", "expected"), [
    (["0x0", "0x0"], False),
    (["0x50000", "0x50000"], False),             # latched before this session began
    (["0x0", "0x50000"], True),                  # a dip between samples, latched during it
    (["0x0", "0x50005", "0x50000"], True),       # seen as it happened
])
def test_a_supply_that_sagged_during_the_session_is_flagged(tmp_path: Path, flags, expected) -> None:
    recorder = SessionRecorder(tmp_path, META)
    for value in flags:
        recorder.record_system({"throttled_flags": value})
    assert recorder.close()["under_voltage"] is expected


def test_a_cpu_session_records_whether_a_hat_was_fitted(tmp_path: Path) -> None:
    """A fitted HAT idles and draws power that an INA219 counts in a CPU baseline."""
    fitted = {**CPU_META, "accelerator": {**CPU_META["accelerator"], "hailo": {"present": True}}}
    assert SessionRecorder(tmp_path, fitted).close()["hat_fitted"] is True
    assert SessionRecorder(tmp_path, CPU_META).close()["hat_fitted"] is False


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


def test_a_session_cut_off_by_a_power_loss_is_closed_as_interrupted(tmp_path: Path, clock: Clock) -> None:
    from datetime import datetime, timezone

    SessionRecorder(tmp_path, META, now=datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)).close()
    cut = SessionRecorder(tmp_path, META, now=datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc))
    for index in range(5):
        cut.record_inference({"frame_seq": index, "infer_ms": 10.0})
        cut.record_system({"power_w": 5.0})
        if index == 2:
            cut.write_summary()  # the last summary before the cut: 2 s stale
        clock.now += 1.0
    cut.flush()
    # The power goes: no close(), a row half written, a summary rewrite unfinished.
    with (cut.directory / "inference.csv").open("a") as handle:
        handle.write("2026-11-14T09:00:05.000+00:00,5.0,0,5,0.5,1")
    (cut.directory / "summary.json.tmp").write_text('{"sess')

    assert recover_interrupted(tmp_path) == [cut.session_id]

    inference = _rows(cut.directory / "inference.csv")
    assert [row["frame_seq"] for row in inference] == ["0", "1", "2", "3", "4"]
    last = max(inference[-1]["timestamp"], _rows(cut.directory / "system.csv")[-1]["timestamp"])
    summary = json.loads((cut.directory / "summary.json").read_text())
    assert summary["status"] == "interrupted"
    assert summary["ended_at"] == last
    assert summary["duration_s"] == pytest.approx(4.0)  # from the rows, not the stale summary
    assert not (cut.directory / "summary.json.tmp").exists()
    assert [row["status"] for row in _rows(tmp_path / "sessions.csv")] == ["complete", "interrupted"]
    assert recover_interrupted(tmp_path) == []  # already closed


def test_trimming_reads_back_past_a_long_torn_tail(tmp_path: Path) -> None:
    from pepperwatch.session import _trim_torn_row

    path = tmp_path / "rows.csv"
    path.write_bytes(b"timestamp,elapsed_s\r\nA,1\r\nB,2\r\n" + b"\0" * 100)
    assert _trim_torn_row(path, block=16) == {"timestamp": "B", "elapsed_s": "2"}
    assert path.read_bytes() == b"timestamp,elapsed_s\r\nA,1\r\nB,2\r\n"
    path.write_bytes(b"timestamp,elapsed_s\r\nA,")
    assert _trim_torn_row(path) is None and path.read_bytes() == b"timestamp,elapsed_s\r\n"


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
