"""Exercise the camera, accelerator detection and sensors on this Pi.

    python -m pepperwatch.hal.selftest [--seconds 5] [--still]

Lists every camera found, says whether an AI HAT+ is usable, opens the camera
the worker would use and measures the frame rate it actually achieves, then
prints one telemetry sample. Run it after plugging in a camera or the HAT: it
is the fastest way to tell a hardware problem from a code problem.
"""

from __future__ import annotations

import argparse
import logging
import time

from ..accel import choose_accelerator
from ..config import load
from . import discover_cameras, get_telemetry, open_camera


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=5.0, help="How long to sample (default 5)")
    parser.add_argument("--still", action="store_true", help="Also grab a high-resolution still")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    base = load()
    choice = choose_accelerator(base)
    print(f"accel    : {choice.name} — {choice.reason}")
    cfg = base.with_profile(choice.profile)
    print(f"profile  : {choice.profile} (preview target {cfg.camera.preview_fps} fps)")

    found = discover_cameras()
    print(f"cameras  : {len(found)} found")
    for candidate in found:
        print(f"           {candidate.describe()}")

    camera, _ = open_camera(cfg)
    telemetry = get_telemetry(cfg)

    with camera:
        info = camera.info
        print(f"camera   : {info.kind} — {info.description}")
        print(f"           {info.width}x{info.height} target {info.fps:.1f} fps")
        for key, value in sorted(info.detail.items()):
            print(f"           {key}: {value}")

        frames = 0
        empties = 0
        first_seq = None
        last_shape = None
        started = time.monotonic()
        deadline = started + args.seconds
        while time.monotonic() < deadline:
            frame = camera.read()
            if frame is None:
                empties += 1
                time.sleep(0.002)
                continue
            frames += 1
            last_shape = frame.bgr.shape
            if first_seq is None:
                first_seq = frame.seq
        elapsed = time.monotonic() - started

        achieved = frames / elapsed if elapsed else 0.0
        target = info.fps or achieved
        drift = 100.0 * (achieved - target) / target if target else 0.0
        print(f"frames   : {frames} in {elapsed:.2f}s -> {achieved:.1f} fps ({drift:+.1f}% vs target)")
        print(f"           {empties} empty reads (normal: that is inter-frame pacing)")
        print(f"           shape {last_shape}, seq {first_seq}..{first_seq + frames - 1 if first_seq else '-'}")

        if args.still:
            began = time.monotonic()
            still = camera.capture_still()
            print(f"still    : {still.shape} in {1000 * (time.monotonic() - began):.0f} ms")

    sample = telemetry.sample()
    print("telemetry:")
    for key, value in sample.as_dict().items():
        shown = "—" if value is None else value
        print(f"           {key:<16} {shown}")

    telemetry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
