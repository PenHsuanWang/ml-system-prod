"""Local neural bundle and recovery integration; synthetic data is not model qualification."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.domain.datasets.ebm import EBM_DATASET_SCHEMA, EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.ml import MLError, SchemaMismatch
from src.domain.training.ebm import EBMTrainingConfig
from src.infrastructure.datasets.ebm_snapshots import EBMFeatureSnapshot, EBMFileDatasetSource
from src.infrastructure.serving.ebm_artifacts import EBMArtifactVerifier
from src.infrastructure.serving.ebm_runtime import EBMRuntime
from src.plugins.models.ebm_cnn_v1 import EBMModelConfig, build_ebm_cnn
from src.plugins.training.ebm_trainer_v1 import EBMTrainerV1

torch = pytest.importorskip("torch")
pytestmark = pytest.mark.torch
CODE_DIGEST = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[3]


class InterruptedTraining(Exception):
    pass


def stop_after_first_checkpoint(step: int, metrics: dict[str, float]) -> None:
    assert step == 0 and np.isfinite(metrics["loss"])
    raise InterruptedTraining


def metadata() -> dict[str, Any]:
    return {
        "schema_version": EBM_DATASET_SCHEMA,
        "signal_order": list(EBM_SIGNAL_ORDER),
        "pad_order": list(EBM_PAD_ORDER),
        "feature_config": {"version": "fdc_spectrogram_v1", "log1p": True},
        "samples": [
            {
                "sample_id": f"wafer-{i}", "lot_id": f"lot-{i}",
                "wafer_id": "1", "recipe_id": "recipe-a",
                "segment_id": 1 if i < 6 else 2 if i < 8 else 3,
                "event_time": f"2026-01-{i + 1:02d}T00:00:00Z",
                "available_at": None, "cdf_target": 0.1 + i * 0.08,
                "pad_counts": [float(i + 1)] * 4,
            }
            for i in range(10)
        ],
        "splits": {"train": list(range(6)), "validation": [6, 7], "test": [8, 9]},
    }


@pytest.fixture(scope="module")  # type: ignore[misc]
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> EBMFeatureSnapshot:
    source = EBMFileDatasetSource(tmp_path_factory.mktemp("ebm-runtime-snapshot"))
    arrays = np.random.default_rng(30).uniform(0.0, 2.0, (10, 7, 4, 9, 31)).astype(np.float32)
    return source.load(source.publish(metadata(), arrays))


@pytest.fixture(scope="module")  # type: ignore[misc]
def trained(
    tmp_path_factory: pytest.TempPathFactory, snapshot: EBMFeatureSnapshot
) -> dict[str, Any]:
    output = tmp_path_factory.mktemp("ebm-trained-bundle")
    return EBMTrainerV1().train(
        EBMTrainingConfig(epochs=1, batch_size=3, pair_sample_size=16),
        output, lambda step, values: None, dataset=snapshot, code_digest=CODE_DIGEST,
    )


@pytest.fixture  # type: ignore[misc]
def bundle(tmp_path: Path, trained: dict[str, Any]) -> tuple[Path, str]:
    copied = tmp_path / "bundle"
    shutil.copytree(trained["bundle_path"], copied)
    return copied, trained["manifest_digest"]


def runtime(bundle: tuple[Path, str]) -> EBMRuntime:
    path, expected = bundle
    return EBMRuntime(path, expected, build_ebm_cnn, expected_code_digest=CODE_DIGEST)


def predict(handle: EBMRuntime, value: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = handle.predict(
        value, signal_order=EBM_SIGNAL_ORDER, pad_order=EBM_PAD_ORDER
    )
    return results


def change_trusted_manifest(
    bundle: tuple[Path, str], change: Callable[[dict[str, Any]], None]
) -> tuple[Path, str]:
    """Recompute the trusted outer digest to exercise semantic checks beyond hashes."""
    directory, _ = bundle
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))
    return directory, hashlib.sha256(path.read_bytes()).hexdigest()


def test_training_publishes_independent_golden_bundle(
    trained: dict[str, Any], bundle: tuple[Path, str], snapshot: EBMFeatureSnapshot
) -> None:
    assert len(trained["metrics"]) == 1
    assert np.isfinite(trained["metrics"][0]["loss"])
    assert trained["quality_decision"] == "NOT_EVALUATED_BY_POLICY"
    manifest = EBMArtifactVerifier().verify(*bundle)
    assert manifest["lineage"]["dataset_digest"] == snapshot.content_digest
    assert manifest["recipe"]["semantics"] == "retrospective_replay"
    handle = runtime(bundle)
    output = predict(handle, snapshot.spectrograms[list(snapshot.metadata.validation)])
    golden = json.loads((bundle[0] / "golden_io.json").read_text())
    assert len(output) == len(golden["outputs"]) == 2
    for actual, expected in zip(output, golden["outputs"], strict=True):
        assert actual["score_kind"] == "uncalibrated_latent_mean_plus_std"
        assert len(actual["latent"]) == 112
        for key in ("mu", "sigma", "raw_nn_score", "latent"):
            np.testing.assert_allclose(actual[key], expected[key], rtol=0, atol=1e-5)
        assert actual["raw_nn_score"] == pytest.approx(actual["mu"] + 1.645 * actual["sigma"])
    handle.close()
    with pytest.raises(MLError, match="closed"):
        predict(handle, snapshot.spectrograms[:1])


@pytest.mark.parametrize("case", ["signal_order", "pad_order", "shape", "dtype", "nan", "empty"])  # type: ignore[misc]
def test_prediction_rejects_wrong_input_contract(
    bundle: tuple[Path, str], snapshot: EBMFeatureSnapshot, case: str
) -> None:
    handle = runtime(bundle)
    value = snapshot.spectrograms[:1].copy()
    signals, pads = EBM_SIGNAL_ORDER, EBM_PAD_ORDER
    if case == "signal_order":
        signals = tuple(reversed(signals))
    elif case == "pad_order":
        pads = tuple(reversed(pads))
    elif case == "shape":
        value = value[:, :, :, :, :-1]
    elif case == "dtype":
        value = value.astype(np.float64)
    elif case == "nan":
        value[0, 0, 0, 0, 0] = np.nan
    elif case == "empty":
        value = value[:0]
    with pytest.raises(SchemaMismatch):
        handle.predict(value, signal_order=signals, pad_order=pads)


def test_epoch_checkpoint_resume_restores_dropout_optimizer_and_sampler(
    tmp_path: Path, snapshot: EBMFeatureSnapshot
) -> None:
    config = EBMTrainingConfig(epochs=3, batch_size=3, pair_sample_size=16, seed=71)
    model_config = EBMModelConfig(
        channels=(4,), kernel_sizes=((3, 3),), strides=((1, 1),), dropout=0.35,
    )
    trainer = EBMTrainerV1()
    full = trainer.train(config, tmp_path / "full", lambda step, values: None,
                         dataset=snapshot, code_digest=CODE_DIGEST, model_config=model_config)
    interrupted = tmp_path / "interrupted"
    with pytest.raises(InterruptedTraining):
        trainer.train(config, interrupted, stop_after_first_checkpoint,
                      dataset=snapshot, code_digest=CODE_DIGEST, model_config=model_config)
    assert (interrupted / "checkpoint.pt").is_file()
    assert not (interrupted / "bundle").exists()
    original_spec = (interrupted / "training_spec.json").read_bytes()
    resumed_steps: list[int] = []
    resumed = trainer.train(
        config, interrupted, lambda step, values: resumed_steps.append(step),
        dataset=snapshot, code_digest=CODE_DIGEST, model_config=model_config,
        resume=interrupted / "checkpoint.pt",
    )
    assert resumed_steps == [1, 2]
    assert (interrupted / "training_spec.json").read_bytes() == original_spec
    assert resumed["training_spec_digest"] == full["training_spec_digest"]
    assert resumed["metrics"] == full["metrics"]
    full_state = torch.load(Path(full["bundle_path"]) / "weights.pt", weights_only=True)
    resumed_state = torch.load(Path(resumed["bundle_path"]) / "weights.pt", weights_only=True)
    assert full_state.keys() == resumed_state.keys()
    for name, value in full_state.items():
        torch.testing.assert_close(resumed_state[name], value, rtol=0, atol=0)


def test_platform_spec_digest_is_bound_to_export_and_resume(
    tmp_path: Path, snapshot: EBMFeatureSnapshot
) -> None:
    config = EBMTrainingConfig(epochs=2, batch_size=3, pair_sample_size=16)
    trainer = EBMTrainerV1()
    output = tmp_path / "platform-run"
    platform_digest = "c" * 64
    with pytest.raises(InterruptedTraining):
        trainer.train(config, output, stop_after_first_checkpoint, dataset=snapshot,
                      code_digest=CODE_DIGEST, platform_spec_digest=platform_digest)
    before = {str(path.relative_to(output)): path.read_bytes()
              for path in output.rglob("*") if path.is_file()}
    with pytest.raises(MLError, match="mismatch"):
        trainer.train(config, output, lambda step, values: None, dataset=snapshot,
                      code_digest=CODE_DIGEST, platform_spec_digest="d" * 64,
                      resume=output / "checkpoint.pt")
    assert {str(path.relative_to(output)): path.read_bytes()
            for path in output.rglob("*") if path.is_file()} == before
    result = trainer.train(config, output, lambda step, values: None, dataset=snapshot,
                           code_digest=CODE_DIGEST, platform_spec_digest=platform_digest,
                           resume=output / "checkpoint.pt")
    exported = json.loads((output / "bundle" / "manifest.json").read_text())
    checkpoint = torch.load(output / "checkpoint.pt", weights_only=True)
    assert result["training_spec_digest"] == platform_digest
    assert exported["lineage"]["training_spec_digest"] == platform_digest
    assert result["trainer_spec_digest"] == exported["lineage"]["trainer_spec_digest"]
    assert checkpoint["spec_digest"] == result["trainer_spec_digest"] != platform_digest


@pytest.mark.parametrize("value", ["", "A" * 64, "g" * 64, "a" * 63])  # type: ignore[misc]
def test_invalid_platform_digest_is_rejected_before_output(
    tmp_path: Path, snapshot: EBMFeatureSnapshot, value: str
) -> None:
    output = tmp_path / "invalid-platform-digest"
    with pytest.raises(MLError, match="platform"):
        EBMTrainerV1().train(EBMTrainingConfig(), output, lambda step, values: None,
                             dataset=snapshot, code_digest=CODE_DIGEST, platform_spec_digest=value)
    assert not output.exists()


@pytest.mark.parametrize("case", ["dataset", "training_config", "model_config", "code"])  # type: ignore[misc]
def test_resume_rejects_changed_bound_inputs(
    tmp_path: Path, snapshot: EBMFeatureSnapshot, case: str
) -> None:
    trainer = EBMTrainerV1()
    config = EBMTrainingConfig(epochs=2, batch_size=3, pair_sample_size=16)
    interrupted = tmp_path / "interrupted"
    with pytest.raises(InterruptedTraining):
        trainer.train(config, interrupted, stop_after_first_checkpoint,
                      dataset=snapshot, code_digest=CODE_DIGEST)
    selected_dataset, selected_code, model_config = snapshot, CODE_DIGEST, EBMModelConfig()
    if case == "dataset":
        source = EBMFileDatasetSource(tmp_path / "changed-snapshot")
        changed = copy.deepcopy(snapshot.metadata.to_dict())
        changed["samples"][0]["cdf_target"] = 0.25
        selected_dataset = source.load(source.publish(changed, snapshot.spectrograms))
    elif case == "training_config":
        config = replace(config, learning_rate=0.002)
    elif case == "model_config":
        model_config = EBMModelConfig(dropout=0.2)
    elif case == "code":
        selected_code = "sha256:" + "b" * 64
    original_files = {
        str(path.relative_to(interrupted)): path.read_bytes()
        for path in interrupted.rglob("*") if path.is_file()
    }
    for output in (interrupted, tmp_path / "fresh-resume"):
        with pytest.raises(MLError, match="mismatch"):
            trainer.train(config, output, lambda step, values: None,
                          dataset=selected_dataset, code_digest=selected_code,
                          model_config=model_config, resume=interrupted / "checkpoint.pt")
        assert {
            str(path.relative_to(interrupted)): path.read_bytes()
            for path in interrupted.rglob("*") if path.is_file()
        } == original_files
    assert not (tmp_path / "fresh-resume").exists()


@pytest.mark.parametrize("case", ["missing_file", "invalid_model", "invalid_rng"])  # type: ignore[misc]
def test_invalid_resume_state_preserves_existing_run_files(
    tmp_path: Path, snapshot: EBMFeatureSnapshot, case: str
) -> None:
    trainer = EBMTrainerV1()
    config = EBMTrainingConfig(epochs=2, batch_size=3, pair_sample_size=16)
    output = tmp_path / "interrupted"
    with pytest.raises(InterruptedTraining):
        trainer.train(config, output, stop_after_first_checkpoint,
                      dataset=snapshot, code_digest=CODE_DIGEST)
    checkpoint = tmp_path / "invalid-checkpoint.pt"
    if case != "missing_file":
        saved = torch.load(output / "checkpoint.pt", weights_only=True)
        if case == "invalid_model":
            saved["model"] = {}
        else:
            del saved["torch_rng"]
        torch.save(saved, checkpoint)
    original_files = {
        str(path.relative_to(output)): path.read_bytes()
        for path in output.rglob("*") if path.is_file()
    }
    with pytest.raises(MLError, match="checkpoint"):
        trainer.train(config, output, lambda step, values: None,
                      dataset=snapshot, code_digest=CODE_DIGEST, resume=checkpoint)
    assert {
        str(path.relative_to(output)): path.read_bytes()
        for path in output.rglob("*") if path.is_file()
    } == original_files


@pytest.mark.parametrize("filename", ["normalization.npz", "weights.pt"])  # type: ignore[misc]
def test_tampered_bundle_is_rejected_before_weight_deserialization(
    bundle: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    payload = bundle[0] / filename
    changed = bytearray(payload.read_bytes())
    changed[len(changed) // 2] ^= 1
    payload.write_bytes(changed)

    def forbidden_load(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Torch deserialization must not run before all payload digests are verified")

    monkeypatch.setattr(torch, "load", forbidden_load)
    with pytest.raises(MLError, match="verification"):
        runtime(bundle)


@pytest.mark.parametrize("case", ["code", "architecture", "device", "torch_version", "profile"])  # type: ignore[misc]
def test_wrong_code_or_runtime_is_rejected_before_deserialization(
    bundle: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    def change(manifest: dict[str, Any]) -> None:
        if case == "code":
            manifest["model"]["trusted_code_digest"] = "sha256:" + "b" * 64
        elif case == "architecture":
            manifest["model"]["architecture_key"] = "unapproved_model"
        elif case == "device":
            manifest["runtime"]["device"] = "cuda"
        elif case == "torch_version":
            manifest["runtime"]["torch_version"] = "999.0.0"
        elif case == "profile":
            manifest["runtime"]["compatibility_id"] = "unknown"

    changed = change_trusted_manifest(bundle, change)

    def forbidden_load(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Incompatible code/runtime must fail before loading weights")

    monkeypatch.setattr(torch, "load", forbidden_load)
    with pytest.raises(MLError, match="contract"):
        runtime(changed)


def test_wrong_golden_prediction_is_rejected_even_with_valid_payload_digests(
    bundle: tuple[Path, str]
) -> None:
    path = bundle[0] / "golden_io.json"
    golden = json.loads(path.read_text())
    golden["outputs"][0]["mu"] += 1
    path.write_text(json.dumps(golden))

    def change(manifest: dict[str, Any]) -> None:
        entry = next(item for item in manifest["artifact"]["files"] if item["path"] == path.name)
        entry.update(size_bytes=path.stat().st_size,
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    changed = change_trusted_manifest(bundle, change)
    with pytest.raises(MLError, match="golden"):
        runtime(changed)


def test_clean_process_serves_with_only_bundle_and_no_training_or_spark_imports(
    bundle: tuple[Path, str], tmp_path: Path
) -> None:
    code = """
import builtins
import json
import sys
from pathlib import Path

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith(('pyspark', 'src.infrastructure.datasets', 'src.plugins.training')):
        raise AssertionError('Serving tried to access training or Spark code: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import

import numpy as np
import torch
torch.set_num_threads(1)
from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn
from src.infrastructure.serving.ebm_runtime import EBMRuntime
bundle = Path(sys.argv[1])
handle = EBMRuntime(bundle, sys.argv[2], build_ebm_cnn, expected_code_digest=sys.argv[3])
with np.load(bundle / 'golden_input.npz', allow_pickle=False) as archive:
    value = archive['spectrograms']
outputs = handle.predict(value, signal_order=handle.signal_order, pad_order=handle.pad_order)
assert all(len(row['latent']) == 112 for row in outputs)
assert 'pyspark' not in sys.modules
print(json.dumps({'predictions': len(outputs), 'quality': 'NOT_EVALUATED_BY_POLICY'}))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT), environment.get("PYTHONPATH")))
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(bundle[0]), bundle[1], CODE_DIGEST],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["predictions"] == 2
