#!/usr/bin/env python3
"""Benchmark the detection pipeline and write Chapter IV's numbers to CSV.

This is the canonical implementation of manuscript §3.5.1's "Python time module
embedded within the YOLOv8n execution loop to record the exact time delay
between frame capture, inference execution, and bounding-box rendering." It
times every stage separately, because a single end-to-end figure cannot tell
you whether a shortfall is the model, the JPEG encoder, or the camera.

Three different things get called "FPS", and conflating them is how a paper ends
up defending a number it never measured:

``capture_fps``
    How fast frames arrive. Bounded by the camera, not the model.
``inference_fps``
    How fast the model alone can consume frames. **This is the figure §3.2's
    17 FPS requirement is really about.**
``pipeline_fps``
    Sustained rate through the whole loop — preprocess, infer, decode, NMS,
    severity, encode. What the device actually achieves.

All three are reported side by side. Quote ``pipeline_fps`` if you quote one.

    python scripts/bench_fps.py                       # quick default sweep
    python scripts/bench_fps.py --sizes 320,416,640 --threads 1,2,3,4
    python scripts/bench_fps.py --seconds 60 --out benchmarks/pi5_int8.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pepperwatch.config import load  # noqa: E402
from pepperwatch.detect import severity_from_config  # noqa: E402
from pepperwatch.detect.postprocess import letterbox, to_tensor  # noqa: E402
from pepperwatch.hal import get_camera, get_telemetry  # noqa: E402
from pepperwatch.render import encode_jpeg  # noqa: E402

log = logging.getLogger("bench")

STAGES = ("preprocess", "infer", "decode", "severity", "encode")


@dataclass
class StageTimings:
    """Per-stage millisecond samples."""

    samples: dict[str, list[float]] = field(
        default_factory=lambda: {stage: [] for stage in STAGES}
    )

    def add(self, stage: str, ms: float) -> None:
        self.samples[stage].append(ms)

    def summary(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for stage, values in self.samples.items():
            out[f"{stage}_ms_mean"] = round(statistics.fmean(values), 3) if values else 0.0
            out[f"{stage}_ms_p95"] = (
                round(float(np.percentile(values, 95)), 3) if values else 0.0
            )
        return out


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {key: 0.0 for key in ("mean", "p50", "p90", "p95", "p99", "min", "max")}
    array = np.asarray(values)
    return {
        "mean": round(float(array.mean()), 3),
        "p50": round(float(np.percentile(array, 50)), 3),
        "p90": round(float(np.percentile(array, 90)), 3),
        "p95": round(float(np.percentile(array, 95)), 3),
        "p99": round(float(np.percentile(array, 99)), 3),
        "min": round(float(array.min()), 3),
        "max": round(float(array.max()), 3),
    }


def read_throttled() -> str:
    """``vcgencmd get_throttled`` on a Pi, empty elsewhere.

    A benchmark that does not record whether the SoC throttled mid-run is not
    reporting a property of the software.
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
        )
        return result.stdout.strip().split("=")[-1] if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def describe_host() -> dict[str, str]:
    model = ""
    try:
        model = (
            Path("/proc/device-tree/model").read_bytes().decode("utf-8", "replace").rstrip("\x00")
        )
    except OSError:
        model = platform.machine()
    return {
        "host_model": model,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": str(psutil.cpu_count(logical=True)),
        "opencv": cv2.__version__,
    }


def build_detector(cfg, size: int, threads: int):
    """Detector honouring the sweep's size and thread count."""
    model_path = cfg.model_path
    class_names = list(cfg.inference.class_names)

    if model_path.exists():
        from pepperwatch.detect.onnx_detector import OnnxDetector

        return OnnxDetector(
            model_path,
            class_names,
            input_size=size,
            conf_threshold=cfg.inference.conf_threshold,
            iou_threshold=cfg.inference.iou_threshold,
            max_detections=cfg.inference.max_detections,
            intra_op_threads=threads,
        )

    from pepperwatch.detect.stub_detector import StubDetector

    return StubDetector(
        class_names,
        input_size=size,
        conf_threshold=cfg.inference.conf_threshold,
        max_detections=cfg.inference.max_detections,
    )


def measure_capture_only(cfg, seconds: float) -> float:
    """Camera throughput with no inference attached, as the upper bound."""
    camera = get_camera(cfg)
    camera.start()
    frames = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            if camera.read() is not None:
                frames += 1
            else:
                time.sleep(0.001)
    finally:
        camera.stop()
    elapsed = time.monotonic() - started
    return frames / elapsed if elapsed else 0.0


def run_case(cfg, *, size: int, threads: int, seconds: float, quality: int) -> dict:
    """One sweep cell: a sustained pipeline run at a given size and thread count."""
    camera = get_camera(cfg)
    telemetry = get_telemetry(cfg)
    detector = build_detector(cfg, size, threads)
    info = detector.info

    camera.start()
    timings = StageTimings()
    pipeline_ms: list[float] = []
    infer_ms: list[float] = []
    temps: list[float] = []
    detection_counts: list[int] = []
    jpeg_bytes: list[int] = []

    process = psutil.Process()
    peak_rss_mb = process.memory_info().rss / 1e6

    try:
        # Warm up outside the measured window: the first ONNX call allocates
        # arenas and selects kernels, and that cost is not steady state.
        primer = None
        deadline = time.monotonic() + 5.0
        while primer is None and time.monotonic() < deadline:
            primer = camera.read()
        if primer is None:
            raise RuntimeError("no frame from the camera")
        detector.warmup(primer.bgr, rounds=3)

        started = time.monotonic()
        frames = 0
        while time.monotonic() - started < seconds:
            frame = camera.read()
            if frame is None:
                time.sleep(0.001)
                continue

            loop_began = time.perf_counter()

            if info.kind == "onnx":
                # Time the model in isolation from its pre/post-processing.
                mark = time.perf_counter()
                canvas, transform = letterbox(frame.bgr, size)
                tensor = to_tensor(canvas)
                timings.add("preprocess", (time.perf_counter() - mark) * 1000)

                mark = time.perf_counter()
                raw = detector._session.run(  # noqa: SLF001 — deliberate, to isolate the model
                    [detector._output_name], {detector._input_name: tensor}  # noqa: SLF001
                )
                model_ms = (time.perf_counter() - mark) * 1000
                timings.add("infer", model_ms)
                infer_ms.append(model_ms)

                mark = time.perf_counter()
                from pepperwatch.detect.postprocess import decode_yolov8

                detections = decode_yolov8(
                    raw[0], transform, info.class_names,
                    conf_threshold=cfg.inference.conf_threshold,
                    iou_threshold=cfg.inference.iou_threshold,
                    max_detections=cfg.inference.max_detections,
                )
                timings.add("decode", (time.perf_counter() - mark) * 1000)
            else:
                mark = time.perf_counter()
                detections = detector.detect(frame.bgr)
                model_ms = (time.perf_counter() - mark) * 1000
                timings.add("preprocess", 0.0)
                timings.add("infer", model_ms)
                timings.add("decode", 0.0)
                infer_ms.append(model_ms)

            mark = time.perf_counter()
            severity_from_config(detections, cfg)
            timings.add("severity", (time.perf_counter() - mark) * 1000)

            mark = time.perf_counter()
            jpeg = encode_jpeg(frame.bgr, quality)
            timings.add("encode", (time.perf_counter() - mark) * 1000)

            pipeline_ms.append((time.perf_counter() - loop_began) * 1000)
            detection_counts.append(len(detections))
            jpeg_bytes.append(len(jpeg))
            frames += 1

            if frames % 20 == 0:
                peak_rss_mb = max(peak_rss_mb, process.memory_info().rss / 1e6)
                sample = telemetry.sample()
                if sample.cpu_temp_c is not None:
                    temps.append(sample.cpu_temp_c)

        elapsed = time.monotonic() - started
    finally:
        camera.stop()
        telemetry.close()
        detector.close()

    pipeline_fps = frames / elapsed if elapsed else 0.0
    inference_fps = 1000.0 / statistics.fmean(infer_ms) if infer_ms else 0.0
    row = {
        "input_size": size,
        "threads": threads,
        "seconds": round(elapsed, 2),
        "frames": frames,
        "pipeline_fps": round(pipeline_fps, 2),
        "inference_fps": round(inference_fps, 2),
        "meets_17fps_pipeline": pipeline_fps >= 17.0,
        "meets_17fps_inference": inference_fps >= 17.0,
        "detector": info.kind,
        "model_version": info.model_version,
        "synthetic": info.synthetic,
        "peak_rss_mb": round(peak_rss_mb, 1),
        "mean_detections": round(statistics.fmean(detection_counts), 2) if detection_counts else 0.0,
        "mean_jpeg_kb": round(statistics.fmean(jpeg_bytes) / 1024, 1) if jpeg_bytes else 0.0,
        "cpu_temp_start_c": round(temps[0], 1) if temps else None,
        "cpu_temp_peak_c": round(max(temps), 1) if temps else None,
        "throttled": read_throttled(),
    }
    for key, value in percentiles(pipeline_ms).items():
        row[f"pipeline_ms_{key}"] = value
    for key, value in percentiles(infer_ms).items():
        row[f"infer_ms_{key}"] = value
    row.update(timings.summary())
    return row


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--seconds", type=float, default=15.0, help="Seconds per case (default 15)")
    parser.add_argument("--sizes", default=None, help="Comma-separated input sizes (default: configured size)")
    parser.add_argument("--threads", default=None, help="Comma-separated ONNX thread counts")
    parser.add_argument("--quality", type=int, default=None, help="JPEG quality (default: configured)")
    parser.add_argument("--out", type=Path, default=None, help="CSV path (default: benchmarks/<timestamp>.csv)")
    parser.add_argument("--no-capture-baseline", action="store_true", help="Skip the camera-only measurement")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load()

    sizes = (
        [int(s) for s in args.sizes.split(",")] if args.sizes else [cfg.inference.input_size]
    )
    threads = (
        [int(t) for t in args.threads.split(",")]
        if args.threads
        else [cfg.inference.intra_op_threads]
    )
    quality = args.quality if args.quality is not None else cfg.camera.jpeg_quality

    host = describe_host()
    print(f"host     : {host['host_model']}  ({host['cpu_count']} logical cores)")
    print(f"platform : {cfg.platform_kind}   python {host['python']}   opencv {host['opencv']}")
    print(f"model    : {cfg.model_path if cfg.model_path.exists() else 'none (stub detector)'}")
    print(f"sweep    : sizes {sizes} x threads {threads}, {args.seconds:.0f}s each")
    print()

    capture_fps = 0.0
    if not args.no_capture_baseline:
        capture_fps = measure_capture_only(cfg, min(args.seconds, 5.0))
        print(f"capture-only baseline: {capture_fps:.2f} fps (camera ceiling, no inference)\n")

    rows: list[dict] = []
    header = f"{'size':>5} {'thr':>4} {'pipe fps':>9} {'infer fps':>10} {'p95 ms':>8} {'rss MB':>7} {'temp':>6}"
    print(header)
    print("-" * len(header))

    for size in sizes:
        for thread_count in threads:
            row = run_case(cfg, size=size, threads=thread_count, seconds=args.seconds, quality=quality)
            row["capture_fps"] = round(capture_fps, 2)
            row.update(host)
            rows.append(row)
            temp = row["cpu_temp_peak_c"]
            print(
                f"{size:>5} {thread_count:>4} {row['pipeline_fps']:>9.2f} "
                f"{row['inference_fps']:>10.2f} {row['pipeline_ms_p95']:>8.2f} "
                f"{row['peak_rss_mb']:>7.1f} {(f'{temp:.0f}C' if temp else '-'):>6}"
            )

    out = args.out or Path("benchmarks") / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nwrote {out} ({len(rows)} rows, {len(fieldnames)} columns)")

    best = max(rows, key=lambda r: r["pipeline_fps"])
    print(
        f"best     : {best['pipeline_fps']:.2f} pipeline fps at "
        f"{best['input_size']}px / {best['threads']} threads"
    )
    if any(row["synthetic"] for row in rows):
        print("NOTE     : stub detector — these are NOT model timings. Not for Chapter IV.")
    elif not best["meets_17fps_pipeline"]:
        print(
            f"NOTE     : best pipeline rate {best['pipeline_fps']:.2f} fps is below the "
            "§3.2 target of 17 — report the measured value and the size/rate tradeoff."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
