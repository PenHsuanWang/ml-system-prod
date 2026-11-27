"""Type-check the complete EBM slice, including its explicitly typed dependencies."""

import subprocess
import sys
from pathlib import Path

TARGETS = [
    "src/domain/ml.py",
    "src/domain/pagination.py",
    "src/domain/datasets/lineage.py",
    "src/domain/datasets/build_jobs.py",
    "src/domain/training/ebm.py",
    "src/domain/training/workloads.py",
    "src/domain/datasets/ebm.py",
    "src/domain/datasets/ebm_builder.py",
    "src/domain/prediction/ebm.py",
    "src/domain/prediction/models.py",
    "src/application/datasets",
    "src/application/ml_services.py",
    "src/application/prediction",
    "src/api/ml_schemas.py",
    "src/api/collection_schemas.py",
    "src/api/datasets",
    "src/api/ebm_schemas.py",
    "src/infrastructure/datasets/ebm_builder.py",
    "src/infrastructure/persistence/ml_datasets.py",
    "src/infrastructure/persistence/dataset_lineage_schema.py",
    "src/infrastructure/persistence/dataset_build_schema.py",
    "src/infrastructure/persistence/dataset_build_jobs.py",
    "src/infrastructure/datasets/ebm_snapshots.py",
    "src/infrastructure/storage_errors.py",
    "src/infrastructure/datasets/ml_recipes.py",
    "src/infrastructure/serving/artifacts.py",
    "src/infrastructure/serving/ebm_artifacts.py",
    "src/infrastructure/serving/ebm_runtime.py",
    "src/infrastructure/serving/bounded_ebm_prediction.py",
    "src/infrastructure/serving/materializer.py",
    "src/infrastructure/serving/evaluation.py",
    "src/plugins/features",
    "src/plugins/models/ebm_cnn_v1.py",
    "src/plugins/evaluation",
    "src/plugins/training/ebm_trainer_v1.py",
    "src/plugins/training/ebm_losses_v1.py",
    "src/plugins/training/grouped_rank_sampler.py",
    "bootstrap/dataset_builder_cli.py",
    "bootstrap/dataset_build_wiring.py",
    "bootstrap/dataset_build_worker.py",
    "bootstrap/ebm_e2e.py",
    "bootstrap/ml_training_runner.py",
    "bootstrap/ebm_cli.py",
]


def main() -> int:
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--follow-imports=skip", *TARGETS],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
