"""Frame transport from the worker process to the web process.

The worker owns the camera and the annotated-frame JPEG; Flask has to serve
that JPEG to browsers without ever making the worker wait on a socket. Two
backends implement the same interface:

``shm``
    POSIX shared memory holding two slots behind a small header, coordinated by
    a **seqlock**. The writer fills the idle slot, then publishes it by bumping
    the header last; a reader takes the header, copies the slot, and re-reads
    the header to confirm the sequence did not move under it. If it did, the
    read is retried. No mutex, so a reader that is descheduled mid-copy can
    never block the writer — which is the property that matters, because the
    writer is the inference loop and the readers are phones on a field Wi-Fi
    link.

``tmpfs``
    A file per frame published by atomic rename. Simpler to explain and to
    inspect with ordinary tools, at the cost of a syscall pair per frame and
    inode churn. Kept because it is a genuinely useful fallback and because
    benchmarking the two against each other is nearly free.

Both work on macOS and Linux, so the choice is a config value rather than a
platform fork.
"""

from __future__ import annotations

import logging
import os
import struct
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from multiprocessing import resource_tracker, shared_memory
from pathlib import Path

log = logging.getLogger(__name__)

# Header: magic, version, slot, seq, length, capacity, timestamp, two fps floats.
# "<4sHHQIIdff" packs to 40 bytes; padded to 64 so a slot starts cache-aligned
# and the layout has room to grow without invalidating existing segments.
_HEADER_FORMAT = "<4sHHQIIdff"
_HEADER_SIZE = 64
_MAGIC = b"PWF1"
_VERSION = 1
_SLOTS = 2
_READ_RETRIES = 8

# macOS caps POSIX shared-memory object names at 31 bytes including the leading
# slash it prepends (SHM_NAME_MAX), and over-running that raises ENAMETOOLONG
# rather than anything a caller would think to catch. Linux allows far more, so
# validating against the stricter limit keeps one config value portable.
_MAX_SHM_NAME = 30


def _check_shm_name(name: str) -> None:
    if not name:
        raise ValueError("[frames].shm_name must not be empty")
    if len(name) > _MAX_SHM_NAME:
        raise ValueError(
            f"[frames].shm_name {name!r} is {len(name)} characters; "
            f"macOS allows at most {_MAX_SHM_NAME}"
        )


@dataclass(frozen=True, slots=True)
class FramePacket:
    """A published JPEG plus the metadata the dashboard shows beside it."""

    seq: int
    ts: float
    jpeg: bytes
    preview_fps: float
    inference_fps: float

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.ts)


class FrameWriter(ABC):
    @abstractmethod
    def publish(self, jpeg: bytes, *, seq: int, preview_fps: float, inference_fps: float) -> None:
        """Make this JPEG the current frame. Must not block on readers."""

    @abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self) -> "FrameWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FrameReader(ABC):
    @abstractmethod
    def read(self) -> FramePacket | None:
        """The current frame, or ``None`` if nothing has been published yet."""

    @abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self) -> "FrameReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- shared memory ---------------------------------------------------------


def _segment_size(slot_bytes: int) -> int:
    return _HEADER_SIZE + _SLOTS * slot_bytes


class ShmFrameWriter(FrameWriter):
    """Writes frames into a two-slot shared-memory segment."""

    def __init__(self, name: str, slot_bytes: int) -> None:
        _check_shm_name(name)
        self._slot_bytes = int(slot_bytes)
        self._name = name
        size = _segment_size(self._slot_bytes)

        # A segment can survive a crashed worker, so adopt and reset an existing
        # one of the right size rather than failing to start.
        try:
            self._shm = shared_memory.SharedMemory(name=name, create=True, size=size)
            self._owner = True
        except FileExistsError:
            existing = shared_memory.SharedMemory(name=name, create=False)
            if existing.size < size:
                existing.close()
                existing.unlink()
                self._shm = shared_memory.SharedMemory(name=name, create=True, size=size)
                self._owner = True
            else:
                self._shm = existing
                self._owner = True
                log.info("adopted existing frame segment %s", name)

        self._slot = 0
        self._write_header(seq=0, length=0, ts=0.0, preview_fps=0.0, inference_fps=0.0, slot=0)
        log.info(
            "frame transport: shm %s (%d slots x %d KB)", name, _SLOTS, self._slot_bytes // 1024
        )

    def publish(self, jpeg: bytes, *, seq: int, preview_fps: float, inference_fps: float) -> None:
        if len(jpeg) > self._slot_bytes:
            # Dropping one frame is strictly better than corrupting the segment
            # or stalling the inference loop on a reallocation.
            log.warning(
                "frame %d is %d bytes, over the %d-byte slot — dropped; "
                "raise [frames].slot_bytes or lower [camera].jpeg_quality",
                seq, len(jpeg), self._slot_bytes,
            )
            return

        target = 1 - self._slot
        offset = _HEADER_SIZE + target * self._slot_bytes
        self._shm.buf[offset : offset + len(jpeg)] = jpeg

        # Publish last: until the header names this slot and sequence, a reader
        # cannot observe the bytes just written.
        self._write_header(
            seq=seq,
            length=len(jpeg),
            ts=time.time(),
            preview_fps=preview_fps,
            inference_fps=inference_fps,
            slot=target,
        )
        self._slot = target

    def _write_header(
        self, *, seq: int, length: int, ts: float, preview_fps: float, inference_fps: float, slot: int
    ) -> None:
        packed = struct.pack(
            _HEADER_FORMAT,
            _MAGIC, _VERSION, slot, seq, length, self._slot_bytes, ts, preview_fps, inference_fps,
        )
        self._shm.buf[0 : len(packed)] = packed

    def close(self) -> None:
        try:
            self._shm.close()
            if self._owner:
                self._shm.unlink()
        except (FileNotFoundError, BufferError):
            pass


class ShmFrameReader(FrameReader):
    """Reads frames from the writer's segment under a seqlock."""

    def __init__(self, name: str) -> None:
        _check_shm_name(name)
        self._name = name
        self._shm: shared_memory.SharedMemory | None = None
        self._warned = False

    def _attach(self) -> bool:
        if self._shm is not None:
            return True
        try:
            self._shm = shared_memory.SharedMemory(name=self._name, create=False)
        except FileNotFoundError:
            # The ordinary case: the web process outlives or precedes the
            # worker. Not worth logging — it happens once per poll until the
            # worker comes up.
            return False
        except OSError as exc:
            # Anything else (permissions, a name the platform rejects) is a
            # real misconfiguration, but the dashboard must still serve history
            # rather than crash, so warn once and keep degrading gracefully.
            if not self._warned:
                log.warning("cannot attach frame segment %s: %s", self._name, exc)
                self._warned = True
            return False

        # The reader is not the owner. Python's resource_tracker assumes any
        # process that attaches should clean up at exit, which would unlink the
        # live segment out from under the worker when the web process restarts.
        try:
            resource_tracker.unregister(self._shm._name, "shared_memory")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — best effort; harmless if the internals move
            pass
        return True

    def read(self) -> FramePacket | None:
        if not self._attach():
            return None
        assert self._shm is not None

        for _ in range(_READ_RETRIES):
            try:
                magic, version, slot, seq, length, capacity, ts, preview_fps, inference_fps = (
                    struct.unpack(_HEADER_FORMAT, bytes(self._shm.buf[0 : struct.calcsize(_HEADER_FORMAT)]))
                )
            except (ValueError, BufferError):
                return None

            if magic != _MAGIC or version != _VERSION:
                return None
            if seq == 0 or length == 0:
                return None  # nothing published yet

            offset = _HEADER_SIZE + slot * capacity
            payload = bytes(self._shm.buf[offset : offset + length])

            # Re-read the sequence. Unchanged means the writer did not swap
            # slots during the copy, so the payload is internally consistent.
            after_seq = struct.unpack_from("<Q", bytes(self._shm.buf[0:24]), 8)[0]
            if after_seq == seq:
                return FramePacket(
                    seq=seq,
                    ts=ts,
                    jpeg=payload,
                    preview_fps=preview_fps,
                    inference_fps=inference_fps,
                )

        # Losing the race eight times running means the writer is publishing far
        # faster than this reader can copy; the next poll will do.
        return None

    def close(self) -> None:
        if self._shm is not None:
            try:
                self._shm.close()
            except BufferError:
                pass
            self._shm = None


# -- tmpfs -----------------------------------------------------------------


class TmpfsFrameWriter(FrameWriter):
    """Publishes each frame as a file, made visible by atomic rename."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._current = self._dir / "current.jpg"
        self._meta = self._dir / "current.meta"
        log.info("frame transport: tmpfs %s", self._dir)

    def publish(self, jpeg: bytes, *, seq: int, preview_fps: float, inference_fps: float) -> None:
        # Write to a temporary neighbour and rename: on POSIX the rename is
        # atomic, so a reader either sees the whole previous frame or the whole
        # new one, never a half-written file.
        fd, temp_name = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(jpeg)
            os.replace(temp_name, self._current)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

        fd, temp_name = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{seq} {time.time()} {preview_fps} {inference_fps}")
        os.replace(temp_name, self._meta)

    def close(self) -> None:
        for path in (self._current, self._meta):
            path.unlink(missing_ok=True)


class TmpfsFrameReader(FrameReader):
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._current = self._dir / "current.jpg"
        self._meta = self._dir / "current.meta"

    def read(self) -> FramePacket | None:
        try:
            jpeg = self._current.read_bytes()
            fields = self._meta.read_text().split()
        except (FileNotFoundError, OSError):
            return None
        if not jpeg or len(fields) != 4:
            return None

        return FramePacket(
            seq=int(fields[0]),
            ts=float(fields[1]),
            jpeg=jpeg,
            preview_fps=float(fields[2]),
            inference_fps=float(fields[3]),
        )

    def close(self) -> None:
        pass


# -- factory ---------------------------------------------------------------


def make_writer(cfg) -> FrameWriter:
    if cfg.frames.transport == "tmpfs":
        return TmpfsFrameWriter(Path(cfg.frames.tmpfs_dir))
    return ShmFrameWriter(cfg.frames.shm_name, cfg.frames.slot_bytes)


def make_reader(cfg) -> FrameReader:
    if cfg.frames.transport == "tmpfs":
        return TmpfsFrameReader(Path(cfg.frames.tmpfs_dir))
    return ShmFrameReader(cfg.frames.shm_name)
