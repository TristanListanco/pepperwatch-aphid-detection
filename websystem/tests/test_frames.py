"""Frame transport correctness, including the seqlock under a live writer.

The property under test is that a reader never observes a torn frame — half of
frame N and half of frame N+1. To make tearing detectable, the writer publishes
payloads whose every byte is derived from the sequence number, so any mixture of
two frames is immediately visible as a byte that does not match the header's
sequence.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from pepperwatch.config import load
from pepperwatch.frames import (
    ShmFrameReader,
    ShmFrameWriter,
    TmpfsFrameReader,
    TmpfsFrameWriter,
    make_reader,
    make_writer,
)

pytestmark = pytest.mark.timeout(90)

SLOT_BYTES = 262144


def payload_for(seq: int, length: int) -> bytes:
    """A payload whose content is fully determined by ``seq``."""
    return bytes([seq % 251]) * length


def _writer_process(name: str, slot_bytes: int, frames: int, ready) -> None:
    writer = ShmFrameWriter(name, slot_bytes)
    ready.set()
    try:
        for seq in range(1, frames + 1):
            # Vary the length so a stale longer frame cannot masquerade as a
            # complete shorter one.
            length = 4096 + (seq * 997) % 60000
            writer.publish(
                payload_for(seq, length), seq=seq, preview_fps=25.0, inference_fps=8.5
            )
            time.sleep(0.001)
    finally:
        # The reader owns cleanup in this test, so release without unlinking.
        writer._owner = False  # noqa: SLF001
        writer.close()


# -- shared memory ---------------------------------------------------------


def test_shm_returns_none_before_anything_is_published() -> None:
    name = f"pw_test_empty_{int(time.time() * 1000) % 100000}"
    writer = ShmFrameWriter(name, SLOT_BYTES)
    reader = ShmFrameReader(name)
    try:
        assert reader.read() is None
    finally:
        reader.close()
        writer.close()


def test_shm_reader_returns_none_when_no_segment_exists() -> None:
    """The web process starts before the worker; it must not crash."""
    reader = ShmFrameReader("pw_test_absent_seg")
    assert reader.read() is None
    reader.close()


def test_shm_name_over_name_max_is_rejected_at_construction() -> None:
    """A shm object is a file in /dev/shm, so NAME_MAX (255) bounds its name.

    Over-running it raises ENAMETOOLONG, which nothing would think to catch, so
    the limit is checked up front and reported against the config key that set it.
    """
    too_long = "pw_" + "x" * 300
    with pytest.raises(ValueError, match=r"shm_name"):
        ShmFrameReader(too_long)
    with pytest.raises(ValueError, match=r"at most 255"):
        ShmFrameWriter(too_long, SLOT_BYTES)


def test_configured_default_shm_name_is_within_the_limit() -> None:
    """Guards the shipped default against drifting over the platform limit."""
    from pepperwatch.frames import _MAX_SHM_NAME

    assert len(load(use_env=False).frames.shm_name) <= _MAX_SHM_NAME


def test_shm_roundtrip_preserves_payload_and_metadata() -> None:
    name = f"pw_test_rt_{int(time.time() * 1000) % 100000}"
    writer = ShmFrameWriter(name, SLOT_BYTES)
    reader = ShmFrameReader(name)
    try:
        writer.publish(b"\xff\xd8hello\xff\xd9", seq=42, preview_fps=24.5, inference_fps=7.25)
        packet = reader.read()
        assert packet is not None
        assert packet.jpeg == b"\xff\xd8hello\xff\xd9"
        assert packet.seq == 42
        assert packet.preview_fps == pytest.approx(24.5)
        assert packet.inference_fps == pytest.approx(7.25)
        assert packet.age_s < 5.0
    finally:
        reader.close()
        writer.close()


def test_shm_alternates_slots_so_the_live_frame_is_never_overwritten() -> None:
    """A reader copying frame N must not have its bytes rewritten by N+1."""
    name = f"pw_test_slots_{int(time.time() * 1000) % 100000}"
    writer = ShmFrameWriter(name, SLOT_BYTES)
    try:
        slots = []
        for seq in range(1, 5):
            writer.publish(payload_for(seq, 1000), seq=seq, preview_fps=25.0, inference_fps=8.0)
            slots.append(writer._slot)  # noqa: SLF001
        assert slots == [1, 0, 1, 0]
    finally:
        writer.close()


def test_shm_drops_an_oversized_frame_rather_than_corrupting_the_segment() -> None:
    name = f"pw_test_big_{int(time.time() * 1000) % 100000}"
    writer = ShmFrameWriter(name, 8192)
    reader = ShmFrameReader(name)
    try:
        writer.publish(payload_for(1, 4000), seq=1, preview_fps=25.0, inference_fps=8.0)
        writer.publish(payload_for(2, 99999), seq=2, preview_fps=25.0, inference_fps=8.0)

        packet = reader.read()
        assert packet is not None
        # The good frame survives; the oversized one was refused.
        assert packet.seq == 1
        assert packet.jpeg == payload_for(1, 4000)
    finally:
        reader.close()
        writer.close()


def test_shm_never_yields_a_torn_frame_under_a_live_writer() -> None:
    """The seqlock assertion, against a real concurrent writer process."""
    name = f"pw_test_race_{int(time.time() * 1000) % 100000}"
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_writer_process, args=(name, SLOT_BYTES, 1500, ready))
    proc.start()
    assert ready.wait(timeout=20), "writer process never started"

    reader = ShmFrameReader(name)
    reads = 0
    torn: list[str] = []
    seen_sequences = set()
    deadline = time.monotonic() + 6.0

    try:
        while time.monotonic() < deadline and proc.is_alive():
            packet = reader.read()
            if packet is None:
                continue
            reads += 1
            seen_sequences.add(packet.seq)
            expected_byte = packet.seq % 251
            if any(byte != expected_byte for byte in packet.jpeg):
                mixed = sorted(set(packet.jpeg))
                torn.append(f"seq {packet.seq}: expected {expected_byte}, saw bytes {mixed[:4]}")
    finally:
        reader.close()
        proc.join(timeout=20)
        if proc.is_alive():
            proc.terminate()
        # The writer released without unlinking; clean up here.
        cleanup = ShmFrameWriter(name, SLOT_BYTES)
        cleanup.close()

    assert torn == [], f"observed torn frames: {torn[:3]}"
    assert reads > 50, f"only completed {reads} reads — the race was not exercised"
    assert len(seen_sequences) > 10, f"only saw {len(seen_sequences)} distinct frames"


# -- tmpfs -----------------------------------------------------------------


def test_tmpfs_roundtrip(tmp_path: Path) -> None:
    writer = TmpfsFrameWriter(tmp_path / "frames")
    reader = TmpfsFrameReader(tmp_path / "frames")
    try:
        assert reader.read() is None
        writer.publish(b"\xff\xd8abc\xff\xd9", seq=7, preview_fps=20.0, inference_fps=6.5)
        packet = reader.read()
        assert packet is not None
        assert packet.jpeg == b"\xff\xd8abc\xff\xd9"
        assert packet.seq == 7
        assert packet.inference_fps == pytest.approx(6.5)
    finally:
        reader.close()
        writer.close()


def test_tmpfs_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    """Atomic rename means the temp neighbour must never survive."""
    directory = tmp_path / "frames"
    writer = TmpfsFrameWriter(directory)
    try:
        for seq in range(1, 30):
            writer.publish(payload_for(seq, 2048), seq=seq, preview_fps=25.0, inference_fps=8.0)
        assert list(directory.glob("*.tmp")) == []
        assert sorted(p.name for p in directory.iterdir()) == ["current.jpg", "current.meta"]
    finally:
        writer.close()


# -- factory ---------------------------------------------------------------


@pytest.mark.parametrize("transport", ["shm", "tmpfs"])
def test_factory_pairs_match_across_both_backends(transport: str, tmp_path: Path) -> None:
    """Identical behaviour through the interface, whichever backend is configured."""
    cfg = load(
        {
            "frames": {
                "transport": transport,
                "shm_name": f"pw_test_factory_{transport}_{int(time.time() * 1000) % 100000}",
                "slot_bytes": SLOT_BYTES,
                "tmpfs_dir": str(tmp_path / "frames"),
            }
        },
        use_env=False,
    )
    writer = make_writer(cfg)
    reader = make_reader(cfg)
    try:
        assert reader.read() is None
        writer.publish(b"\xff\xd8payload\xff\xd9", seq=3, preview_fps=25.0, inference_fps=9.0)
        packet = reader.read()
        assert packet is not None
        assert packet.seq == 3
        assert packet.jpeg == b"\xff\xd8payload\xff\xd9"
    finally:
        reader.close()
        writer.close()


def test_reader_follows_a_segment_the_worker_recreated() -> None:
    """A new session (or a worker restart) unlinks and recreates the segment.

    The web process must notice and reattach, or it serves the old session's
    last frame forever.
    """
    name = f"pw_test_re_{int(time.time() * 1000) % 100000}"
    first = ShmFrameWriter(name, SLOT_BYTES)
    reader = ShmFrameReader(name)
    try:
        first.publish(b"old-frame", seq=7, preview_fps=25.0, inference_fps=5.0)
        assert reader.read().jpeg == b"old-frame"

        first.close()
        second = ShmFrameWriter(name, SLOT_BYTES)
        try:
            second.publish(b"new-frame", seq=1, preview_fps=60.0, inference_fps=30.0)
            reader._checked_at = 0.0  # noqa: SLF001 — skip the once-a-second throttle
            packet = reader.read()
            assert packet is not None and packet.jpeg == b"new-frame"
        finally:
            second.close()
    finally:
        reader.close()
