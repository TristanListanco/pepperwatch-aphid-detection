"""Build a synthetic YOLOv8-shaped ONNX model for testing.

This is not a trained network and detects nothing meaningful. Its whole purpose
is to have the *shape* of a real YOLOv8 export — input ``(1, 3, S, S)`` float32,
output ``(1, 4 + num_classes, anchors)`` — so :class:`OnnxDetector`'s session
setup, tensor layout, head decode, letterbox inverse, and NMS can all be
exercised end to end before the aphid model finishes training.

Two anchors carry planted boxes at known coordinates; the rest are below
threshold. That makes the decode assertions exact rather than approximate.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def build(
    path: Path,
    *,
    size: int = 640,
    num_classes: int = 2,
    anchors: int = 16,
    planted: tuple[tuple[float, float, float, float, int, float], ...] = (
        (320.0, 320.0, 64.0, 64.0, 1, 0.88),
        (160.0, 120.0, 40.0, 30.0, 1, 0.51),
    ),
) -> Path:
    """Write the fixture model and return its path."""
    rows = 4 + num_classes
    head = np.zeros((1, rows, anchors), dtype=np.float32)

    # Everything defaults to a low-confidence healthy prediction, so the planted
    # boxes are the only ones that clear a 0.25 threshold.
    head[0, 0, :] = size / 2.0
    head[0, 1, :] = size / 2.0
    head[0, 2, :] = 8.0
    head[0, 3, :] = 8.0
    head[0, 4, :] = 0.02

    for index, (cx, cy, bw, bh, class_id, score) in enumerate(planted):
        head[0, 0, index] = cx
        head[0, 1, index] = cy
        head[0, 2, index] = bw
        head[0, 3, index] = bh
        head[0, 4:, index] = 0.01
        head[0, 4 + class_id, index] = score

    constant = numpy_helper.from_array(head, name="head_const")

    # The graph must consume its input or ORT may prune it and reject the feed;
    # reducing to a scalar and scaling by zero keeps the dependency real while
    # leaving the planted output exactly as authored.
    nodes = [
        helper.make_node("ReduceMean", ["images"], ["img_mean"], keepdims=0, name="reduce"),
        helper.make_node("Mul", ["img_mean", "zero"], ["zeroed"], name="zero_out"),
        helper.make_node("Add", ["head_const", "zeroed"], ["output0"], name="add_dep"),
    ]

    graph = helper.make_graph(
        nodes,
        "pepperwatch_fixture",
        inputs=[helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, size, size])],
        outputs=[helper.make_tensor_value_info("output0", TensorProto.FLOAT, [1, rows, anchors])],
        initializer=[
            constant,
            numpy_helper.from_array(np.array(0.0, dtype=np.float32), name="zero"),
        ],
    )

    model = helper.make_model(
        graph,
        producer_name="pepperwatch-tests",
        opset_imports=[helper.make_opsetid("", 18)],
    )
    model.ir_version = 9  # what onnxruntime 1.22 accepts
    onnx.checker.check_model(model)

    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))
    return path


if __name__ == "__main__":
    out = build(Path("var/fixture/fixture.onnx"))
    print(f"wrote {out} ({out.stat().st_size} bytes)")
