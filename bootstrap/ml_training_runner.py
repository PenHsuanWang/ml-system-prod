"""Trusted child entry point. No HTTP, queue consumers or database credentials."""

import argparse
import json
import os
import resource
from pathlib import Path

from src.infrastructure.execution.local_process import process_identity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    args = parser.parse_args()
    root = args.directory.resolve(strict=True)
    (root/"process.json").write_text(json.dumps(process_identity(os.getpid())))
    resource.setrlimit(resource.RLIMIT_CPU, (args.cpu_seconds, args.cpu_seconds+1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    # RSS is enforced by the role container/cgroup, not by a misleading Python lease.
    code = 1
    try:
        from src.plugins.training.pytorch_trainer import PyTorchTrainer, atomic_json
        import torch
        torch.set_num_interop_threads(1)
        from src.domain.ml import canonical_json, digest
        envelope = json.loads((root/"spec.json").read_text())
        spec = envelope["spec"]
        if digest(["training-spec-v1", canonical_json(spec)]) != envelope["training_spec_digest"]:
            raise ValueError("Execution spec digest mismatch")

        def progress(step: int, values: dict[str, float]) -> None:
            atomic_json(root/"progress.json", {"step": step, "metrics": values})
        from src.infrastructure.datasets.snapshots import FileDatasetSource
        dataset = FileDatasetSource(Path(os.environ["ML_DATASET_ROOT"])).load(spec["dataset"])
        PyTorchTrainer().train(spec, root, progress, producer=envelope["producer"], dataset=dataset)
        code = 0
    finally:
        (root/"exit.json").write_text(json.dumps({"code": code}))


if __name__ == "__main__":
    main()
