"""Audit the bundled ONNX models.

Unlike ``onnx`` (a protobuf tooling dependency that is not shipped), the runtime
already parses everything needed for an audit: I/O shapes and custom metadata.
Runs as a pytest test over ``testdata/model`` and as a CLI dump:

    python tests/test_onnx_audit.py ../testdata/model/yolo11n.onnx
"""

import json
import sys
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "testdata" / "model"


def inspect_model(model_path: Path) -> dict:
    """Load a model with onnxruntime and return its I/O and metadata."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.log_severity_level = 4
    session = ort.InferenceSession(
        str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
    )
    meta = session.get_modelmeta()
    producer = getattr(meta, "producer_name", "") or ""
    version = getattr(meta, "producer_version", "") or ""
    return {
        "inputs": [(i.name, i.shape, i.type) for i in session.get_inputs()],
        "outputs": [(o.name, o.shape, o.type) for o in session.get_outputs()],
        "metadata": dict(meta.custom_metadata_map),
        "producer": f"{producer} {version}".strip(),
    }


def _print_report(model_path: Path, info: dict) -> None:
    print(f"\n{'=' * 60}\n{model_path.name}\n{'=' * 60}")
    print(f"Producer: {info['producer'] or '(unknown)'}")
    for name, shape, dtype in info["inputs"]:
        print(f"  input  {name}: {shape} {dtype}")
    for name, shape, dtype in info["outputs"]:
        print(f"  output {name}: {shape} {dtype}")
    names = info["metadata"].get("names")
    if names:
        try:
            parsed = json.loads(names)
            print(f"  classes: {len(parsed)} -> {list(parsed.values())[:5]}...")
        except (TypeError, ValueError):
            print(f"  names: {names}")
    for key, value in info["metadata"].items():
        if key != "names":
            print(f"  meta {key}: {value}")


@pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="testdata/model is not available")
def test_bundled_models_load_with_shapes():
    """Every bundled model must load on CPU and expose at least one input/output."""
    models = sorted(MODEL_DIR.glob("*.onnx"))
    assert models, f"no .onnx files under {MODEL_DIR}"

    failures = []
    for model_path in models:
        try:
            info = inspect_model(model_path)
        except Exception as exc:  # surfaced in the assertion message
            failures.append(f"{model_path.name}: {exc}")
            continue
        assert info["inputs"], f"{model_path.name} exposes no inputs"
        assert info["outputs"], f"{model_path.name} exposes no outputs"

    assert not failures, "models failed to load:\n" + "\n".join(failures)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else MODEL_DIR
    paths = sorted(target.glob("*.onnx")) if target.is_dir() else [target]
    if not paths:
        print("Usage: python tests/test_onnx_audit.py <model.onnx | model_dir>")
        sys.exit(1)
    for path in paths:
        _print_report(path, inspect_model(path))
