"""EBM admission, immutable tenant registration and approved child dispatch."""

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from src.api.ml_schemas import EBMTrainingIntent, SubmitRequest, TrainingIntent
from src.domain.datasets.ebm import EBM_DATASET_SCHEMA, EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.ml import Conflict, MLError, NotFound, canonical_json, digest
from src.domain.training.workloads import EBM_WORKLOAD, workload_for_spec
from src.infrastructure.datasets.ml_recipes import ApprovedRecipeResolver
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog


def source_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    samples = [{"sample_id": f"row-{index}", "lot_id": f"lot-{index // 2}",
                "recipe_id": "recipe-a", "segment_id": 1, "wafer_id": f"wafer-{index % 2}",
                "event_time": f"2026-01-{index // 2 + 1:02d}T00:00:00Z", "available_at": None,
                "cdf_target": 0.1 + index * 0.05,
                "pad_counts": [float(index // 2 + 1 + pad) for pad in range(4)]}
               for index in range(12)]
    value = {"schema_version": EBM_DATASET_SCHEMA,
             "feature_config": {"version": "fdc_spectrogram_v1", "log1p": True},
             "signal_order": list(EBM_SIGNAL_ORDER), "pad_order": list(EBM_PAD_ORDER),
             "samples": samples,
             "splits": {"train": list(range(6)), "validation": [6, 7], "test": [8, 9, 10, 11]}}
    array = np.repeat(np.arange(6, dtype=np.float32), 2).reshape(12, 1, 1, 1, 1)
    array = np.broadcast_to(array, (12, 7, 4, 9, 31)).copy()
    metadata_path, npz_path = directory / "source.json", directory / "source.npz"
    metadata_path.write_text(json.dumps(value))
    np.savez(npz_path, spectrograms=array)
    return metadata_path, npz_path


def ebm_intent(dataset_id="fdc", version="v1"):
    return EBMTrainingIntent(
        recipe={"id": "ebm_poc_replay_v1", "version": "1.0.0"},
        dataset={"id": dataset_id, "version": version},
        model={"architecture": "ebm_cnn_v1", "config": {"channels": [4, 8]}},
        training={"epochs": 1, "batch_size": 6, "pair_sample_size": 16},
    ).model_dump()


def register(catalog, actor, tmp_path):
    operator = replace(actor, permissions=actor.permissions | {"ml:dataset:manage"})
    paths = source_files(tmp_path / "source")
    return catalog.register_ebm(operator, "fdc", "v1", *paths), operator, paths


def test_public_submission_selects_ebm_defaults_without_changing_mlp():
    ebm = SubmitRequest.model_validate({"spec": {
        "recipe": {"id": "ebm_poc_replay_v1", "version": "1.0.0"},
        "dataset": {"id": "fdc", "version": "v1"},
    }}).spec
    assert isinstance(ebm, EBMTrainingIntent)
    assert ebm.model.architecture == "ebm_cnn_v1"
    assert ebm.training.wafer_rank_margin == 0.3
    assert ebm.resource_profile_id == "cpu-ebm-v1"
    mlp = SubmitRequest.model_validate({"spec": {
        "recipe": {"id": "nn_classifier", "version": "1.0.0"},
        "dataset": {"id": "dataset-demo", "version": "2026-10-28-v1"},
    }}).spec
    assert isinstance(mlp, TrainingIntent)
    assert mlp.model.architecture == "mlp_v1"
    assert mlp.training.loss == "cross_entropy"
    assert mlp.training.epochs == 30


@pytest.mark.parametrize("section,key,value", [
    ("model", "architecture", "module.arbitrary.CNN"),
    ("training", "USE_AUX_HEAD", True),
    ("training", "loss", "arbitrary_loss"),
    ("training", "rank_group_size", 5),
    ("training", "seed", True),
    ("training", "learning_rate", float("nan")),
])
def test_ebm_submission_rejects_unapproved_configuration(section, key, value):
    intent = ebm_intent()
    intent[section][key] = value
    with pytest.raises(ValidationError):
        SubmitRequest.model_validate({"spec": intent})


def test_ebm_catalog_is_immutable_tenant_scoped_and_uses_safe_copy(components, actor, tmp_path):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    registered, operator, paths = register(catalog, actor, tmp_path)
    assert registered.tenant_id == actor.tenant_id
    assert digest(actor.tenant_id) in registered.snapshot_ref
    assert json.loads(registered.summary_json)["dataset_schema_version"] == EBM_DATASET_SCHEMA
    assert catalog.resolve(actor, "fdc", "v1") == registered
    assert catalog.register_ebm(operator, "fdc", "v1", *paths) == registered
    with pytest.raises(NotFound):
        catalog.resolve(replace(actor, tenant_id="other"), "fdc", "v1")
    metadata = json.loads(paths[0].read_text())
    metadata["samples"][0]["cdf_target"] = 0.9
    paths[0].write_text(json.dumps(metadata))
    with pytest.raises(Conflict, match="immutable"):
        catalog.register_ebm(operator, "fdc", "v1", *paths)
    assert catalog.resolve(actor, "fdc", "v1") == registered
    link = tmp_path / "linked.json"
    link.symlink_to(paths[0])
    with pytest.raises(MLError, match="symbolic"):
        catalog.register_ebm(operator, "fdc", "v2", link, paths[1])


def test_resolver_freezes_paired_contract_and_admin_policy(components, actor, tmp_path):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    registered, _, _ = register(catalog, actor, tmp_path)
    policy = {"policy_version": "engineering-explicit-v1", "max_anchor_loss": 2.0,
              "minimum_lot_correlation": 0.5, "minimum_lots": 3}
    resolver = ApprovedRecipeResolver(code_revision="test-code", image_digest="test-image",
                                     catalog=catalog, ebm_evaluation_policy=policy)
    policy["max_anchor_loss"] = 99
    spec = json.loads(resolver.resolve(canonical_json(ebm_intent()), actor))
    assert workload_for_spec(spec) == EBM_WORKLOAD
    assert spec["dataset"]["content_digest"] == registered.content_digest
    assert spec["dataset"]["tenant_id"] == actor.tenant_id
    assert spec["code_revision"] == "test-code"
    assert spec["evaluation_policy"] == "engineering-explicit-v1"
    assert spec["evaluation_policy_config"]["max_anchor_loss"] == 2.0
    unqualified = ApprovedRecipeResolver(code_revision="test", image_digest="test", catalog=catalog)
    bare = json.loads(unqualified.resolve(canonical_json(ebm_intent()), actor))
    assert bare["evaluation_policy"] == "ebm-unqualified-v1"
    assert bare["evaluation_policy_config"] is None
    for mutate in (
        lambda item: item.update(evaluation_policy_config=policy),
        lambda item: item.update(resource_profile_id="cpu-small-v1"),
        lambda item: item["model"].update(architecture="mlp_v1"),
        lambda item: item["model"]["config"].update(QK_DIM=16),
    ):
        invalid = copy.deepcopy(ebm_intent())
        mutate(invalid)
        with pytest.raises(MLError):
            resolver.resolve(canonical_json(invalid), actor)


def test_recipe_cannot_pair_with_another_workloads_dataset(components, actor, tmp_path, intent):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    register(catalog, actor, tmp_path)
    resolver = ApprovedRecipeResolver(code_revision="test", image_digest="test", catalog=catalog)
    wrong_ebm = ebm_intent("dataset-demo", "2026-10-28-v1")
    with pytest.raises(MLError, match="Dataset contract"):
        resolver.resolve(canonical_json(wrong_ebm), actor)
    wrong_mlp = json.loads(intent)
    wrong_mlp["dataset"] = {"id": "fdc", "version": "v1"}
    with pytest.raises(MLError, match="Dataset contract"):
        resolver.resolve(canonical_json(wrong_mlp), actor)


@pytest.mark.torch
def test_child_dispatch_trains_registered_ebm_bundle(components, actor, tmp_path):
    pytest.importorskip("torch")
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    registered, _, _ = register(catalog, actor, tmp_path)
    resolver = ApprovedRecipeResolver(code_revision="test-code", image_digest="test-image",
                                     catalog=catalog)
    spec = json.loads(resolver.resolve(canonical_json(ebm_intent()), actor))
    spec_digest = digest(["training-spec-v1", canonical_json(spec)])
    execution = tmp_path / "execution"
    execution.mkdir()
    (execution / "spec.json").write_text(json.dumps({"spec": spec,
        "training_spec_digest": spec_digest, "producer": {"job_id": "job", "attempt_id": "attempt"}}))
    environment = {**os.environ, "ML_DATASET_ROOT": str(components.settings.dataset_root),
                   "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    process = subprocess.run([sys.executable, "-m", "bootstrap.ml_training_runner",
                              "--directory", str(execution), "--cpu-seconds", "60"],
                             env=environment, capture_output=True, text=True, timeout=90)
    assert process.returncode == 0, process.stderr
    result = json.loads((execution / "result.json").read_text())
    manifest = json.loads((Path(result["bundle_path"]) / "manifest.json").read_text())
    assert manifest["lineage"]["dataset_digest"] == registered.content_digest
    assert manifest["lineage"]["training_spec_digest"] == spec_digest
    assert manifest["model"]["architecture_key"] == "ebm_cnn_v1"
    assert manifest["producer"] == {"job_id": "job", "attempt_id": "attempt"}
    assert json.loads((execution / "exit.json").read_text()) == {"code": 0}
