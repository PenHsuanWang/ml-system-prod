"""Contract/dispatch checks plus bounded execution of the existing trainers."""

import copy
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from bootstrap import ml_training_runner as runner
from src.domain.ml import MLError, canonical_json, digest
from src.domain.training.workloads import (
    APPROVED_WORKLOADS,
    EBM_WORKLOAD,
    MLP_WORKLOAD,
    _build_workload_registry,
    workload_for_spec,
)
from src.infrastructure.datasets.ml_recipes import ApprovedRecipeResolver
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.infrastructure.serving.materializer import verify_bundle
from tests.ml.ebm.test_platform_training import ebm_intent, register

METADATA = {
    "workload_id": "workload_id",
    "evaluator_version": "evaluator_version",
    "compatibility_id": "compatibility_id",
    "input_schema_version": "input_schema_version",
    "feature_schema_version": "input_schema_version",
    "output_contract_version": "output_schema_version",
    "artifact_profile": "artifact_profile",
}


def spec_for(workload):
    return {
        "recipe": {"id": workload.workload_id, "version": "1.0.0"},
        **{field: getattr(workload, attribute) for field, attribute in METADATA.items()},
    }


@pytest.mark.parametrize("workload", [MLP_WORKLOAD, EBM_WORKLOAD])
def test_exact_recipe_and_unchanged_contract_metadata(workload):
    assert workload_for_spec(spec_for(workload)) is workload
    assert APPROVED_WORKLOADS[(workload.workload_id, "1.0.0")] is workload
    expected = {
        "nn_classifier": ("classifier-v1", "torch-cpu-v1", "1", "1", "mlp-neural-bundle-v1"),
        "ebm_poc_replay_v1": (
            "ebm-neural-v1",
            "ebm-torch-cpu-v1",
            "ebm-spectrogram-input-v1",
            "ebm-neural-output-v1",
            "ebm-neural-bundle-v1",
        ),
    }
    assert (
        tuple(value for name, value in asdict(workload).items() if name != "workload_id")
        == (expected[workload.workload_id])
    )
    assert len(APPROVED_WORKLOADS) == 2


@pytest.mark.parametrize(
    "recipe",
    [
        None,
        {},
        "nn_classifier",
        [],
        {"id": "unapproved", "version": "1.0.0"},
        {"id": "nn_classifier", "version": "2.0.0"},
        {"id": "ebm_poc_replay_v1", "version": "1.0"},
        {"id": "nn_classifier", "version": ["1.0.0"]},
        {"id": [], "version": "1.0.0"},
        {"id": "nn_classifier", "version": "1.0.0", "import_path": "untrusted.module"},
    ],
)
def test_unapproved_or_ambiguous_recipe_is_rejected(recipe):
    with pytest.raises(MLError, match="Unapproved workload recipe"):
        workload_for_spec({"recipe": recipe})


@pytest.mark.parametrize("workload", [MLP_WORKLOAD, EBM_WORKLOAD])
@pytest.mark.parametrize("field", METADATA)
def test_legacy_defaults_and_required_ebm_metadata(workload, field):
    spec = spec_for(workload)
    spec.pop(field)
    if workload == MLP_WORKLOAD:
        assert workload_for_spec(spec) is MLP_WORKLOAD
    else:
        with pytest.raises(MLError, match="components do not match"):
            workload_for_spec(spec)
    for invalid in (None, "incompatible", [], False):
        spec[field] = invalid
        with pytest.raises(MLError, match="components do not match"):
            workload_for_spec(spec)


def test_legacy_mlp_spec_can_omit_all_optional_workload_fields():
    assert workload_for_spec({"recipe": {"id": "nn_classifier", "version": "1.0.0"}}) is (
        MLP_WORKLOAD
    )


def test_registries_and_contracts_are_read_only():
    with pytest.raises(TypeError):
        APPROVED_WORKLOADS[("nn_classifier", "1.0.0")] = EBM_WORKLOAD
    with pytest.raises(FrozenInstanceError):
        MLP_WORKLOAD.workload_id = "changed"
    with pytest.raises(TypeError):
        runner.TRAINING_HANDLERS[MLP_WORKLOAD.workload_id] = runner._train_ebm


def test_workload_registration_rejects_duplicate_conflicting_and_invalid_definitions():
    entry = (("nn_classifier", "1.0.0"), MLP_WORKLOAD)
    with pytest.raises(MLError, match="Duplicate"):
        _build_workload_registry([entry, entry])
    with pytest.raises(MLError, match="Conflicting"):
        _build_workload_registry(
            [
                entry,
                (("nn_classifier", "2.0.0"), replace(MLP_WORKLOAD, compatibility_id="changed")),
            ]
        )
    for invalid in (
        (("", "1.0.0"), MLP_WORKLOAD),
        (("nn_classifier", ""), MLP_WORKLOAD),
        (("nn_classifier", "1.0.0"), None),
        (("nn_classifier", "1.0.0"), replace(MLP_WORKLOAD, compatibility_id="")),
    ):
        with pytest.raises(MLError, match="Invalid"):
            _build_workload_registry([invalid])


def test_handler_registration_rejects_duplicate_and_untrusted_definitions():
    entry = (MLP_WORKLOAD.workload_id, runner._train_mlp)
    with pytest.raises(MLError, match="Duplicate"):
        runner._build_training_handlers([entry, entry])
    for invalid in (("unapproved", runner._train_mlp), (MLP_WORKLOAD.workload_id, None)):
        with pytest.raises(MLError, match="Invalid"):
            runner._build_training_handlers([invalid])
    assert runner.TRAINING_HANDLERS == {
        MLP_WORKLOAD.workload_id: runner._train_mlp,
        EBM_WORKLOAD.workload_id: runner._train_ebm,
    }


@pytest.mark.parametrize("workload", [MLP_WORKLOAD, EBM_WORKLOAD])
def test_dispatch_selects_only_the_registered_handler_and_preserves_arguments(
    workload,
    tmp_path,
    monkeypatch,
):
    # A call recorder verifies dispatch only; real training is exercised below.
    calls = []

    def selected(*args):
        calls.append(args)

    monkeypatch.setattr(runner, "TRAINING_HANDLERS", {workload.workload_id: selected})
    spec = spec_for(workload)
    original = copy.deepcopy(spec)

    def progress(step, values):
        pass

    producer = {"job_id": "job", "attempt_id": "attempt"}
    runner.run_training(spec, tmp_path, progress, producer, platform_spec_digest="a" * 64)
    assert calls == [(spec, tmp_path, progress, producer, "a" * 64)]
    assert spec == original


@pytest.mark.parametrize("workload", [MLP_WORKLOAD, EBM_WORKLOAD])
def test_missing_handler_fails_without_fallback_or_dataset_io(workload, tmp_path, monkeypatch):
    def must_not_run(*args):
        raise AssertionError("No trainer may run for a missing handler")

    other_id = EBM_WORKLOAD.workload_id if workload == MLP_WORKLOAD else MLP_WORKLOAD.workload_id
    monkeypatch.setattr(runner, "TRAINING_HANDLERS", {other_id: must_not_run})
    monkeypatch.delenv("ML_DATASET_ROOT", raising=False)
    with pytest.raises(MLError, match="no registered training handler"):
        runner.run_training(
            spec_for(workload), tmp_path, must_not_run, {}, platform_spec_digest="a" * 64
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "workload,profile,architecture",
    [
        (MLP_WORKLOAD, "cpu-ebm-v1", "mlp_v1"),
        (MLP_WORKLOAD, "cpu-small-v1", "ebm_cnn_v1"),
        (EBM_WORKLOAD, "cpu-small-v1", "ebm_cnn_v1"),
        (EBM_WORKLOAD, "cpu-ebm-v1", "mlp_v1"),
    ],
)
def test_handlers_reject_incompatible_profile_or_architecture_before_loading_data(
    workload,
    profile,
    architecture,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ML_DATASET_ROOT", str(tmp_path / "missing"))
    spec = {
        **spec_for(workload),
        "resource_profile_id": profile,
        "model": {"architecture": architecture},
    }
    with pytest.raises(MLError, match="execution profile or model is not approved"):
        runner.run_training(spec, tmp_path, lambda *args: None, {}, platform_spec_digest="a" * 64)


def test_importing_dispatch_does_not_load_heavy_training_dependencies():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bootstrap.ml_training_runner; "
            "assert not {'torch', 'numpy', 'scipy', 'mlflow'} & sys.modules.keys(); "
            "assert 'src.plugins.training.pytorch_trainer' not in sys.modules; "
            "assert 'src.plugins.training.ebm_trainer_v1' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == 0, process.stderr


@pytest.mark.torch
@pytest.mark.parametrize("workload", [MLP_WORKLOAD, EBM_WORKLOAD])
def test_resolved_specs_execute_real_registered_trainers_and_preserve_outputs(
    workload,
    components,
    actor,
    intent,
    tmp_path,
    monkeypatch,
):
    pytest.importorskip("torch")
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    if workload == EBM_WORKLOAD:
        registered, _, _ = register(catalog, actor, tmp_path)
        value = ebm_intent()
    else:
        registered = catalog.resolve(actor, "dataset-demo", "2026-10-28-v1")
        value = json.loads(intent)
        value["training"]["epochs"] = 1
    resolver = ApprovedRecipeResolver(
        code_revision="test-code", image_digest="test-image", catalog=catalog
    )
    spec = json.loads(resolver.resolve(canonical_json(value), actor))
    assert workload_for_spec(spec) is workload
    spec_digest = digest(["training-spec-v1", canonical_json(spec)])
    root = tmp_path / "training"
    root.mkdir()
    monkeypatch.setenv("ML_DATASET_ROOT", str(components.settings.dataset_root))
    events = []
    producer = {"job_id": "registered-handler-job", "attempt_id": "registered-handler-attempt"}
    runner.run_training(
        spec,
        root,
        lambda step, values: events.append((step, values)),
        producer,
        platform_spec_digest=spec_digest,
    )
    result = json.loads((root / "result.json").read_text())
    manifest = verify_bundle(root / "bundle", result["manifest_digest"])
    assert result["dataset_digest"] == registered.content_digest
    assert manifest["lineage"]["dataset_digest"] == registered.content_digest
    assert manifest["lineage"]["training_spec_digest"] == spec_digest
    assert manifest["producer"] == producer
    assert manifest["runtime"]["compatibility_id"] == workload.compatibility_id
    assert manifest["contract"]["input_schema_version"] == workload.input_schema_version
    assert manifest["contract"]["output_schema_version"] == workload.output_schema_version
    assert len(events) == 1 and events[0][0] == 0
    assert result["metrics"] == [{"step": step, **values} for step, values in events]
    assert (root / "checkpoint.pt").is_file()
