#!/usr/bin/env python3
"""Build the mock camera's frame cache from the Naawan field photographs.

The dataset lives in the parent thesis repository as ~1,140 iPhone JPEGs at
5712x4284 and about 4 MB each — far too heavy to decode at preview frame rates,
and currently deleted from the working tree. This script reads them straight out
of git history, corrects EXIF orientation, and writes two pre-scaled caches:

    var/mock_frames/preview/   preview-resolution BGR JPEGs, what MockCamera replays
    var/mock_frames/still/     larger frames, what a manual snapshot returns

Run it once before developing on macOS:

    python scripts/prepare_mock_frames.py

Point it at a directory instead of git with ``--source``, and rebuild after
changing the preview resolution in config (the manifest records what it was
built for, and MockCamera warns when the two disagree).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pepperwatch.config import load  # noqa: E402

# The field dataset, tagged in the parent repo (split over four commits to fit
# GitHub's 2 GB push limit; the tag points at the one containing all of it).
DEFAULT_COMMIT = "datasets-v1"
DATASET_PREFIX = "datasets"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic"}


def git_root() -> Path:
    """The parent thesis repository that holds the dataset."""
    return Path(__file__).resolve().parent.parent.parent


def list_git_images(repo: Path, commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit, "--", DATASET_PREFIX],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        line
        for line in result.stdout.splitlines()
        if Path(line).suffix.lower() in IMAGE_SUFFIXES
    )


def read_git_blob(repo: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        check=True,
    )
    return result.stdout


def interleave_by_collector(paths: list[str]) -> list[str]:
    """Round-robin across collectors so a truncated run stays representative.

    The dataset is three directories of unequal size (589 / 304 / 251). Taking
    the first N in sorted order would yield one person's photos from one
    afternoon in one light; interleaving keeps all three represented.
    """
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(Path(path).parent.name, []).append(path)
    ordered: list[str] = []
    for index in range(max((len(g) for g in groups.values()), default=0)):
        for name in sorted(groups):
            if index < len(groups[name]):
                ordered.append(groups[name][index])
    return ordered


def fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale to cover the target, then centre-crop — no letterbox bars.

    A real camera fills its sensor, so the mock should too. Padding here would
    train the detector-facing code on black bars that never occur in the field.
    """
    return ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def main() -> int:
    cfg = load()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, help="Directory of images; defaults to reading git history")
    parser.add_argument("--commit", default=DEFAULT_COMMIT, help=f"Commit to read (default {DEFAULT_COMMIT})")
    parser.add_argument("--limit", type=int, default=300, help="Max frames to build (default 300)")
    parser.add_argument("--preview-size", default=None, help="WxH; defaults to the configured preview resolution")
    parser.add_argument("--still-size", default="1600x1200", help="WxH for snapshots (default 1600x1200)")
    parser.add_argument("--quality", type=int, default=90, help="Cache JPEG quality (default 90)")
    parser.add_argument("--force", action="store_true", help="Rebuild even if the cache looks current")
    args = parser.parse_args()

    if args.preview_size:
        pw, ph = (int(part) for part in args.preview_size.lower().split("x"))
    else:
        pw, ph = cfg.camera.preview_width, cfg.camera.preview_height
    sw, sh = (int(part) for part in args.still_size.lower().split("x"))

    out_root = cfg.path("var/mock_frames")
    preview_dir = out_root / "preview"
    still_dir = out_root / "still"
    manifest_path = out_root / "manifest.json"

    if manifest_path.exists() and not args.force:
        existing = json.loads(manifest_path.read_text())
        if (
            existing.get("preview_size") == [pw, ph]
            and existing.get("still_size") == [sw, sh]
            and existing.get("count", 0) >= min(args.limit, existing.get("source_count", 0))
        ):
            print(f"cache is current: {existing['count']} frames at {pw}x{ph} — pass --force to rebuild")
            return 0

    # Gather the source list.
    if args.source:
        source_paths = sorted(
            str(p) for p in args.source.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES
        )
        if not source_paths:
            print(f"no images under {args.source}", file=sys.stderr)
            return 1
        reader = lambda path: Path(path).read_bytes()  # noqa: E731
        origin = str(args.source)
    else:
        repo = git_root()
        try:
            source_paths = list_git_images(repo, args.commit)
        except subprocess.CalledProcessError as exc:
            print(f"could not read git history at {repo}: {exc}", file=sys.stderr)
            print("pass --source DIR to build from a directory instead", file=sys.stderr)
            return 1
        if not source_paths:
            print(f"no dataset images at {args.commit}:{DATASET_PREFIX}/", file=sys.stderr)
            return 1
        reader = lambda path: read_git_blob(repo, args.commit, path)  # noqa: E731
        origin = f"git:{args.commit}:{DATASET_PREFIX}"

    source_count = len(source_paths)
    selected = interleave_by_collector(source_paths)[: args.limit]

    for directory in (preview_dir, still_dir):
        directory.mkdir(parents=True, exist_ok=True)
        for stale in directory.glob("*.jpg"):
            stale.unlink()

    print(f"source : {origin}")
    print(f"found  : {source_count} images, building {len(selected)}")
    print(f"preview: {pw}x{ph} -> {preview_dir}")
    print(f"still  : {sw}x{sh} -> {still_dir}")

    built: list[dict[str, object]] = []
    for index, path in enumerate(selected):
        try:
            raw = reader(path)
            with Image.open(__import__("io").BytesIO(raw)) as handle:
                # iPhone photos carry EXIF orientation; without this, a third of
                # the dataset replays sideways.
                image = ImageOps.exif_transpose(handle).convert("RGB")
                name = f"{index:04d}.jpg"
                fit_cover(image, pw, ph).save(preview_dir / name, "JPEG", quality=args.quality)
                fit_cover(image, sw, sh).save(still_dir / name, "JPEG", quality=args.quality)
                built.append({"name": name, "source": path, "collector": Path(path).parent.name})
        except Exception as exc:  # noqa: BLE001 — one bad file must not kill the run
            print(f"  skip {path}: {exc}", file=sys.stderr)
            continue
        if (index + 1) % 25 == 0:
            print(f"  {index + 1}/{len(selected)}")

    if not built:
        print("built nothing", file=sys.stderr)
        return 1

    manifest_path.write_text(
        json.dumps(
            {
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "origin": origin,
                "source_count": source_count,
                "count": len(built),
                "preview_size": [pw, ph],
                "still_size": [sw, sh],
                "quality": args.quality,
                "frames": built,
            },
            indent=2,
        )
    )

    collectors: dict[str, int] = {}
    for entry in built:
        collectors[str(entry["collector"])] = collectors.get(str(entry["collector"]), 0) + 1
    preview_mb = sum(f.stat().st_size for f in preview_dir.glob("*.jpg")) / 1e6
    still_mb = sum(f.stat().st_size for f in still_dir.glob("*.jpg")) / 1e6
    print(f"done   : {len(built)} frames  ({', '.join(f'{k} {v}' for k, v in sorted(collectors.items()))})")
    print(f"size   : preview {preview_mb:.1f} MB, still {still_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
