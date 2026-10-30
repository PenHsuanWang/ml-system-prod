"""Local CPU EBM vertical slice; platform Job/Registry wiring is a later work package."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.training.ebm import EBMTrainingConfig
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.serving.ebm_artifacts import read_json
from src.infrastructure.serving.ebm_runtime import EBMRuntime
from src.plugins.features.fdc_spectrogram_v1 import FdcSpectrogramV1
from src.plugins.models.ebm_cnn_v1 import EBMModelConfig, build_ebm_cnn
from src.plugins.training.ebm_trainer_v1 import EBMTrainerV1
from src.plugins.training.pytorch_trainer import atomic_json


def trusted_code_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    files = ["src/domain/datasets/ebm.py", "src/domain/training/ebm.py",
             "src/plugins/features/fdc_spectrogram_v1.py", "src/plugins/models/ebm_cnn_v1.py",
             "src/plugins/training/ebm_losses_v1.py", "src/plugins/training/grouped_rank_sampler.py",
             "src/plugins/training/ebm_trainer_v1.py", "src/infrastructure/serving/ebm_runtime.py",
             "src/infrastructure/serving/ebm_artifacts.py"]
    value = hashlib.sha256()
    for name in sorted(files):
        value.update(name.encode())
        value.update(b"\0")
        value.update((root / name).read_bytes())
    return value.hexdigest()


def synthetic_snapshot(root: Path) -> dict[str, str]:
    """Authorized synthetic engineering fixture, never a Notebook parity reference."""
    import numpy as np

    samples, traces = [], []
    time = np.arange(256, dtype=np.float32)
    for lot in range(12):
        traces.append({signal: {pad: (np.sin(time * (0.05 + s * .01) + p * .3)
                           * (1 + lot * .05) + lot * .02).tolist()
                      for p, pad in enumerate(EBM_PAD_ORDER)}
                      for s, signal in enumerate(EBM_SIGNAL_ORDER)})
        for wafer in range(2):
            samples.append({"sample_id": f"synthetic-{lot}-{wafer}", "lot_id": f"lot-{lot}",
                "recipe_id": "synthetic-recipe", "segment_id": 1 if lot < 6 else 2,
                "wafer_id": f"wafer-{wafer}", "event_time": f"2026-01-{lot+1:02d}T00:00:00Z",
                "available_at": None, "cdf_target": (lot + 1) / 13 + wafer * .002,
                "pad_counts": [float(lot + p + 1) for p in range(4)]})
    features = FdcSpectrogramV1(EBM_SIGNAL_ORDER).transform_batch(traces)
    features = np.repeat(features, 2, axis=0)
    metadata = {"schema_version": "ebm-feature-snapshot-v1",
                "feature_config": {"version": "fdc_spectrogram_v1", "log1p": True},
                "signal_order": list(EBM_SIGNAL_ORDER), "pad_order": list(EBM_PAD_ORDER),
                "samples": samples, "splits": {"train": list(range(12)),
                    "validation": list(range(12, 18)), "test": list(range(18, 24))}}
    return cast(dict[str, str], EBMFileDatasetSource(root).publish(metadata, features))


def evaluate_bundle(result: dict[str, Any], snapshot: Any, output: Path) -> dict[str, Any]:
    from src.plugins.evaluation.ebm_v1 import evaluate_neural_outputs

    runtime = EBMRuntime(Path(result["bundle_path"]), result["manifest_digest"], build_ebm_cnn,
                         expected_code_digest=trusted_code_digest())
    try:
        indices = snapshot.metadata.test
        predictions = []
        for start in range(0, len(indices), runtime.max_batch):
            batch = list(indices[start:start + runtime.max_batch])
            predictions.extend(runtime.predict(snapshot.spectrograms[batch],
                    signal_order=snapshot.metadata.signal_order, pad_order=snapshot.metadata.pad_order))
        spec = read_json(output / "training_spec.json")
        metrics = evaluate_neural_outputs(predictions, snapshot.metadata, indices,
                        cdf_anchor_eps=spec["training"]["cdf_anchor_eps"])
    finally:
        runtime.close()
    report = {"evaluator_version": "ebm-neural-v1", "manifest_digest": result["manifest_digest"],
              "dataset_digest": snapshot.content_digest, "split": "test", "metrics": metrics,
              "semantics": "retrospective_replay", "policy_version": None,
              "decision": "NOT_EVALUATED_BY_POLICY"}
    atomic_json(output / "evaluation.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke", help="Train and evaluate a small synthetic CPU fixture")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--epochs", type=int, default=1)
    train = commands.add_parser("train", help="Consume an already-published immutable feature snapshot")
    train.add_argument("--dataset-root", type=Path, required=True)
    train.add_argument("--snapshot-ref", required=True)
    train.add_argument("--dataset-digest", required=True)
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--training-config", type=Path)
    train.add_argument("--model-config", type=Path)
    train.add_argument("--resume", type=Path)
    verify = commands.add_parser("verify", help="Verify and load a bundle with no training data")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--manifest-digest", required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        runtime = EBMRuntime(args.bundle, args.manifest_digest, build_ebm_cnn,
                             expected_code_digest=trusted_code_digest())
        runtime.close()
        print(json.dumps({"golden_io": "passed", "manifest_digest": args.manifest_digest}))
        return 0
    output = args.output.resolve()
    if args.command == "smoke":
        source = EBMFileDatasetSource(output / "datasets")
        reference = synthetic_snapshot(output / "datasets")
        config = EBMTrainingConfig(epochs=args.epochs, batch_size=6, pair_sample_size=32)
        model_config = EBMModelConfig(channels=(4, 8))
        resume = None
    else:
        source = EBMFileDatasetSource(args.dataset_root)
        reference = {"snapshot_ref": args.snapshot_ref, "content_digest": args.dataset_digest}
        config = EBMTrainingConfig.from_dict(read_json(args.training_config)) if args.training_config else EBMTrainingConfig()
        model_config = EBMModelConfig.from_dict(read_json(args.model_config)) if args.model_config else EBMModelConfig()
        resume = args.resume
    snapshot = source.load(reference)
    result = EBMTrainerV1().train(config, output, lambda step, metrics: print(
        json.dumps({"epoch": step, **metrics}), file=sys.stderr), dataset=snapshot,
        code_digest=trusted_code_digest(), model_config=model_config, resume=resume,
        producer={"entrypoint": "bootstrap.ebm_cli", "python_version": sys.version.split()[0]})
    evaluation = evaluate_bundle(result, snapshot, output)
    print(json.dumps({**result, "evaluation": evaluation}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
