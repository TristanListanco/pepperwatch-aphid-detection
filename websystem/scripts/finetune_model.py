#!/usr/bin/env python3
"""Fine-tune the detector to stop seeing leaves where there are none.

    pip install ultralytics==8.4.163          # on the GPU machine or Colab
    python scripts/finetune_model.py \\
        --data /path/to/dataset/data.yaml \\
        --negatives /path/to/negatives \\
        --weights models/detect/train-24/weights/best.pt

The deployed model (train-24) calls a green wall "healthy" and brown or black
spots on things that are not leaves "aphid_infested": its validation confusion
matrix shows 241 aphid and 42 healthy boxes on background. It has rarely been
shown what is *not* a leaf, so this trains it on exactly that:

- ``--data`` is the labelled dataset train-24 was trained on, unchanged. The
  model keeps learning the leaves from it; without it, it would forget them.
- ``--negatives`` is a folder of photos with no pepper leaves in them: the
  green wall, brown and black objects, soil, mulch, pots, the frame. They join
  the training set with no labels, which Ultralytics treats as background. One
  in five is held out to measure the result. Clean frames from the Pi's own
  camera are best (``/frame.jpg?camera=0``); the detection log's images have
  boxes drawn on them, so do not use those.

It starts from train-24's weights at a low learning rate, then evaluates both
models the same way: the original validation split (precision, recall, mAP and
boxes on background) and the held-out negatives (boxes found where there is no
leaf). Finally it exports the new model to ONNX for the Pi, exactly as
models/best.onnx was exported. Swap it in only if the table says it is better.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
PI_CONF = 0.25  # the dashboard's default confidence threshold


def split_negatives(folder: Path) -> tuple[list[Path], list[Path]]:
    """Every fifth photo (sorted) is held out; the rest train."""
    photos = sorted(p for p in folder.rglob("*") if p.suffix.lower() in PHOTO_TYPES)
    held_out = photos[4::5]
    train = [p for p in photos if p not in set(held_out)]
    assert train and held_out, f"need at least 5 photos in {folder}, found {len(photos)}"
    return train, held_out


def evaluate(weights: Path, data: str, negatives: list[Path], device, out: Path) -> dict[str, float]:
    from ultralytics import YOLO

    model = YOLO(str(weights))
    # plots=True: Ultralytics only fills the confusion matrix when plotting.
    metrics = model.val(data=data, split="val", device=device, plots=True, verbose=False,
                        project=str(out.parent), name=out.name, exist_ok=True)
    nc = len(model.names)
    matrix = metrics.confusion_matrix.matrix  # rows predicted, columns true; index nc is background
    row = {
        "precision": float(metrics.box.mp),
        "recall": float(metrics.box.mr),
        "mAP50": float(metrics.box.map50),
        "mAP50-95": float(metrics.box.map),
    }
    for index, name in model.names.items():
        row[f"val: {name} boxes on background"] = int(matrix[index, nc])
    boxes = {name: 0 for name in model.names.values()}
    for result in model.predict(source=[str(p) for p in negatives], conf=PI_CONF, device=device,
                                stream=True, verbose=False):
        for cls in result.boxes.cls.tolist():
            boxes[model.names[int(cls)]] += 1
    for name, count in boxes.items():
        row[f"negatives: {name} boxes"] = count
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, help="the labelled dataset's data.yaml")
    parser.add_argument("--negatives", required=True, type=Path, help="folder of photos with no pepper leaves")
    parser.add_argument("--weights", required=True, type=Path, help="train-24's best.pt")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default=None, help="0 for the first GPU, mps on a Mac; default: Ultralytics picks")
    parser.add_argument("--out", type=Path, default=Path("runs/finetune"))
    args = parser.parse_args()

    import yaml
    from ultralytics import YOLO
    from ultralytics.data.utils import check_det_dataset

    dataset = check_det_dataset(args.data)
    base_names = YOLO(str(args.weights)).names
    assert dataset["names"] == base_names, f"dataset classes {dataset['names']} != model's {base_names}"

    # Negatives join the training set as background: images with no label file.
    train_negatives, held_out = split_negatives(args.negatives)
    folder = (args.out / "negatives" / "images").resolve()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for index, photo in enumerate(train_negatives):
        shutil.copy(photo, folder / f"negative_{index:04d}{photo.suffix.lower()}")
    originals = dataset["train"] if isinstance(dataset["train"], list) else [dataset["train"]]
    merged = args.out / "data_with_negatives.yaml"
    merged.write_text(yaml.safe_dump({
        "train": [str(p) for p in originals] + [str(folder)],
        "val": dataset["val"],
        "names": dict(base_names),
    }))
    print(f"{len(train_negatives)} negatives added to training, {len(held_out)} held out")

    print("\nEvaluating the current model (train-24)…")
    out = args.out.resolve()
    before = evaluate(args.weights, args.data, held_out, args.device, out / "val-train24")

    # Low, fixed learning rate: adjust train-24, don't retrain it. The
    # augmentation matches train-24's (its args.yaml).
    YOLO(str(args.weights)).train(
        data=str(merged), epochs=args.epochs, patience=10, imgsz=args.imgsz, batch=args.batch,
        device=args.device, optimizer="AdamW", lr0=0.0003, lrf=0.1, warmup_epochs=1,
        mosaic=0.3, degrees=15.0, fliplr=0.5, close_mosaic=10,
        project=str(out), name="train", exist_ok=True,
    )
    tuned = out / "train" / "weights" / "best.pt"

    print("\nEvaluating the fine-tuned model…")
    after = evaluate(tuned, args.data, held_out, args.device, out / "val-finetuned")

    print(f"\n{'':40} {'train-24':>10} {'fine-tuned':>11}")
    for key in before:
        print(f"{key:40} {before[key]:>10.3f} {after[key]:>11.3f}" if isinstance(before[key], float)
              else f"{key:40} {before[key]:>10} {after[key]:>11}")
    fewer = sum(v for k, v in after.items() if "boxes" in k) < sum(v for k, v in before.items() if "boxes" in k)
    kept = after["mAP50"] >= before["mAP50"] - 0.01
    print("\nBetter: fewer boxes where there is no leaf, leaves still found." if fewer and kept
          else "\nNot clearly better: keep train-24, and look at the table before swapping.")

    onnx = YOLO(str(tuned)).export(format="onnx", imgsz=args.imgsz, opset=12, simplify=True, dynamic=False)
    print(f"\nFor the Pi: {onnx}")
    print("Copy it to websystem/models/pepperwatch.onnx in the Pi's checkout, then sudo ./deploy/install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
