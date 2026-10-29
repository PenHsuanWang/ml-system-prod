"""CPU fixture training, leakage-free transforms and resumable safe checkpoints."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.domain.ml import canonical_json, digest
from src.domain.datasets.models import DatasetSnapshot
from src.plugins.models.mlp import build_mlp


def fixture() -> tuple[list[list[float]], list[int]]:
    rng = random.Random(20261028)
    values = [[rng.uniform(-2, 2), rng.uniform(-2, 2)] for _ in range(512)]
    labels = [int(x[0] + 0.65*x[1] > 0) for x in values]
    return values, labels


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(canonical_json(value))
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


class PyTorchTrainer:
    def train(self, spec: dict[str, Any], directory: Path,
              progress: Callable[[int, dict[str, float]], None],
              *, resume: Path | None = None, producer: dict[str, str] | None = None,
              dataset: DatasetSnapshot | None = None) -> dict[str, object]:
        import torch
        import torch.nn.functional as functional
        torch.set_num_threads(1)
        training = spec["training"]
        torch.manual_seed(training["seed"])
        random.seed(training["seed"])
        raw, labels = fixture()
        if dataset is None:
            dataset = DatasetSnapshot(tuple(tuple(row) for row in raw), tuple(labels),
                                      tuple(range(384)), tuple(range(384, 448)), tuple(range(448, 512)))
        else:
            raw = [list(row) for row in dataset.features]
            labels = list(dataset.labels)
        # Split indices are fixed before fitting transforms; held-out test is untouched.
        train_raw = [raw[i] for i in dataset.train]
        means = [sum(x[i] for x in train_raw)/len(train_raw) for i in range(2)]
        scales = [math.sqrt(sum((x[i]-means[i])**2 for x in train_raw) /
                            len(train_raw)) or 1.0 for i in range(2)]
        normalized = [[(x[i]-means[i])/scales[i] for i in range(2)] for x in raw]
        x = torch.tensor(normalized, dtype=torch.float32)
        y = torch.tensor(labels, dtype=torch.long)
        config = {"input_dim": 2, "hidden_dims": spec["model"]["config"]["hidden_units"],
                  "output_dim": 2, "activation": "relu"}
        model = build_mlp(config)
        optimizer = torch.optim.Adam(model.parameters(), lr=training["learning_rate"])
        start = 0
        checkpoint_path = directory / "checkpoint.pt"
        spec_digest = digest(["training-spec-v1", canonical_json(spec)])
        if resume is not None:
            saved = torch.load(resume, map_location="cpu", weights_only=True)
            if saved["spec_digest"] != spec_digest:
                raise ValueError("Checkpoint spec mismatch")
            model.load_state_dict(saved["model"])
            optimizer.load_state_dict(saved["optimizer"])
            torch.set_rng_state(saved["rng"])
            start = saved["epoch"]+1
        metrics = saved.get("metric_history", []) if resume is not None else []
        for epoch in range(start, training["epochs"]):
            model.train()
            order = torch.tensor(dataset.train)[torch.randperm(len(dataset.train))]
            for indices in order.split(training["batch_size"]):
                optimizer.zero_grad(set_to_none=True)
                loss = functional.cross_entropy(model(x[indices]), y[indices])
                if not torch.isfinite(loss):
                    raise ValueError("Non-finite loss")
                loss.backward()
                optimizer.step()
            model.eval()
            with torch.inference_mode():
                val_logits = model(x[list(dataset.validation)])
                values = {"loss": float(loss), "validation_accuracy":
                          float((val_logits.argmax(1) == y[list(dataset.validation)]).float().mean())}
            progress(epoch, values)
            metrics.append({"step": epoch, **values})
            history = directory/"metrics"
            history.mkdir(exist_ok=True)
            atomic_json(history/f"{epoch:06d}.json", {"step": epoch, **values})
            temporary = directory / "checkpoint.tmp"
            torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(),
                        "epoch": epoch, "rng": torch.get_rng_state(),
                        "metric_history": metrics,
                        "spec_digest": spec_digest}, temporary)
            temporary.replace(checkpoint_path)
        bundle = directory / "bundle"
        bundle.mkdir(exist_ok=True)
        model.eval()
        torch.save(model.state_dict(), bundle/"weights.pt")
        atomic_json(bundle/"preprocessing.json", {"version": "1", "mean": means, "scale": scales,
                    "feature_order": ["sensor_a", "sensor_b"]})
        atomic_json(bundle/"feature_schema.json", {"version": "1", "features":
                    [{"name": name, "type": "float", "minimum": -100, "maximum": 100}
                     for name in ("sensor_a", "sensor_b")]})
        atomic_json(bundle/"output_mapping.json", {"labels": ["negative", "positive"],
                    "score_kind": "uncalibrated_softmax"})
        with torch.inference_mode():
            golden_indices = list(dataset.validation[:4])
            golden = model(x[golden_indices]).softmax(1).tolist()
        atomic_json(bundle/"golden_io.json", {"inputs": [raw[i] for i in golden_indices], "outputs": golden,
                    "absolute_tolerance": 1e-5})
        files = [{"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                  "size_bytes": p.stat().st_size} for p in sorted(bundle.iterdir())]
        manifest: dict[str, Any] = {"manifest_version": "2", "producer": producer or {},
                                    "lineage": {"training_spec_digest": spec_digest,
                                                "dataset_digest": spec["dataset"].get("content_digest", digest([raw, labels])),
                                                "split_indices_digest": digest({"train": dataset.train,
                                                                                "validation": dataset.validation, "test": dataset.test})},
                                    "preprocessing": {"strategy": "standard_scaler_v1", "fit_split": "train",
                                                      "fit_indices_digest": digest(dataset.train)},
                                    "artifact": {"format": "trusted_pytorch_state_dict", "files": files},
                                    "model": {"architecture_key": "mlp_v1", "architecture_version": "1",
                                              "constructor_config": config, "trusted_code_digest": spec["code_revision"]},
                                    "runtime": {"compatibility_id": "torch-cpu-v1",
                                                "training_image_digest": spec["training_image_digest"]},
                                    "contract": {"input_schema_version": "1", "output_schema_version": "1"}}
        atomic_json(bundle/"manifest.json", manifest)
        manifest_digest = hashlib.sha256((bundle/"manifest.json").read_bytes()).hexdigest()
        result = {"bundle_path": str(bundle), "manifest_digest": manifest_digest,
                  "metrics": metrics, "dataset_digest": manifest["lineage"]["dataset_digest"]}
        atomic_json(directory/"result.json", result)
        return result
