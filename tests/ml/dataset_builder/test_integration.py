from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from src.api.ml_schemas import EBMTrainingIntent
from src.application.datasets.builder import (
    EBMDatasetPreparationService,
    RawSnapshotSetResolver,
)
from src.domain.datasets.ebm_builder import (
    EBMDatasetBuilderConfig,
    EBMRawSnapshotReference,
)
from src.domain.ingestion.errors import SnapshotInvalid
from src.domain.ingestion.models import IngestionRequest
from src.domain.ml import Conflict, MLError, NotFound, canonical_json, digest
from src.infrastructure.datasets.ebm_builder import LocalEBMDatasetPublisher
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.datasets.ml_recipes import ApprovedRecipeResolver
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.plugins.features.ebm_dataset_builder_v1 import EBMDatasetBuilderV1
from tests.ml.dataset_builder.conftest import jsonl


def builder_service(components, inputs, tmp_path):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    publisher = LocalEBMDatasetPublisher(catalog, tmp_path / "dataset-builds")
    service = EBMDatasetPreparationService(
        RawSnapshotSetResolver(inputs.ingress.jobs, inputs.ingress.store),
        EBMDatasetBuilderV1(),
        publisher,
    )
    return service, catalog, publisher


def test_resolver_rejects_missing_unpublished_cross_tenant_wrong_digest_and_corruption(
    published_builder_inputs,
):
    inputs = published_builder_inputs
    resolver = RawSnapshotSetResolver(inputs.ingress.jobs, inputs.ingress.store)
    resolved = resolver.resolve(inputs.operator, inputs.references)
    assert resolved.tenant_id == inputs.operator.tenant_id

    with pytest.raises(MLError, match="requires both fdc and wafer"):
        resolver.resolve(
            inputs.operator,
            (inputs.references[0], replace(inputs.references[1], role="fdc")),
        )

    wrong_digest = (
        replace(inputs.references[0], manifest_digest="0" * 64),
        inputs.references[1],
    )
    with pytest.raises(Conflict):
        resolver.resolve(inputs.operator, wrong_digest)
    with pytest.raises(NotFound):
        resolver.resolve(
            inputs.operator,
            (
                replace(inputs.references[0], snapshot_id="missing-snapshot"),
                inputs.references[1],
            ),
        )
    with pytest.raises(NotFound):
        resolver.resolve(replace(inputs.operator, tenant_id="other-tenant"), inputs.references)

    unpublished = inputs.ingress.service.submit(
        inputs.operator, "builder-unpublished", IngestionRequest("builder-fdc", "v1")
    )
    with pytest.raises(NotFound):
        resolver.resolve(
            inputs.operator,
            (
                EBMRawSnapshotReference("fdc", unpublished.snapshot_id, "0" * 64),
                inputs.references[1],
            ),
        )

    payload = Path(inputs.fdc_snapshot.snapshot_ref).parent / "data.jsonl"
    payload.write_bytes(b'{"corrupt":true}\n')
    with pytest.raises(SnapshotInvalid):
        resolver.resolve(inputs.operator, inputs.references)


def test_resolver_rejects_an_aggregate_oversize_set_before_reading_payloads(
    published_builder_inputs,
):
    inputs = published_builder_inputs
    oversized_fdc = replace(inputs.fdc_snapshot, size_bytes=16 * 1024 * 1024)
    snapshots = {
        oversized_fdc.snapshot_id: oversized_fdc,
        inputs.wafer_snapshot.snapshot_id: inputs.wafer_snapshot,
    }

    class MetadataCatalog:
        def snapshot(self, _actor, snapshot_id):
            return snapshots[snapshot_id]

    class NeverReader:
        def read_records(self, _snapshot):
            raise AssertionError("aggregate bounds must be checked before payload reads")

    resolver = RawSnapshotSetResolver(MetadataCatalog(), NeverReader())
    with pytest.raises(MLError, match="snapshot set exceeds"):
        resolver.resolve(inputs.operator, inputs.references)


@pytest.mark.torch
def test_published_raw_snapshots_build_register_resolve_and_train_without_trainer_changes(
    components, published_builder_inputs, tmp_path
):
    pytest.importorskip("torch")
    inputs = published_builder_inputs
    service, catalog, _ = builder_service(components, inputs, tmp_path)
    config = EBMDatasetBuilderConfig("recipe-a", 1, split_seed=17)
    result = service.build(inputs.operator, "ebm-built", "v1", inputs.references, config)

    assert catalog.resolve(inputs.operator, "ebm-built", "v1") == result.dataset
    snapshot = EBMFileDatasetSource(components.settings.dataset_root).load(
        {
            "snapshot_ref": result.dataset.snapshot_ref,
            "content_digest": result.dataset.content_digest,
        }
    )
    assert snapshot.spectrograms.shape == (10, 7, 4, 9, 31)
    receipt_bytes = Path(result.receipt_ref).read_bytes()
    receipt = json.loads(receipt_bytes)
    assert hashlib.sha256(receipt_bytes).hexdigest() == result.receipt_digest
    assert receipt_bytes.decode() == canonical_json(receipt)
    assert receipt["builder"] == {"id": "ebm-dataset-builder", "version": "1.0.0"}
    assert receipt["dataset"] == {
        "id": "ebm-built",
        "version": "v1",
        "content_digest": result.dataset.content_digest,
    }
    assert [(item["role"], item["snapshot_id"]) for item in receipt["inputs"]] == [
        ("fdc", inputs.fdc_snapshot.snapshot_id),
        ("wafer", inputs.wafer_snapshot.snapshot_id),
    ]
    assert receipt["configuration"] == {
        "recipe_id": "recipe-a",
        "segment_id": 1,
        "split_seed": 17,
        "log1p": True,
        "raw_input_schema": "ebm-raw-input-v1",
        "alignment_policy": "common_interval_utc_1hz_linear_no_extrapolation_v1",
        "split_policy": "sha256_lot_approximately_60_20_20_v1",
        "feature_version": "fdc_spectrogram_v1",
    }
    snapshots_by_role = {
        "fdc": inputs.fdc_snapshot,
        "wafer": inputs.wafer_snapshot,
    }
    for receipt_input in receipt["inputs"]:
        source = snapshots_by_role[receipt_input["role"]]
        assert receipt_input == {
            "role": receipt_input["role"],
            "snapshot_id": source.snapshot_id,
            "manifest_digest": source.manifest_digest,
            "content_digest": source.content_digest,
            "source_id": source.source_id,
            "source_version": source.source_version,
            "source_configuration_digest": source.source_configuration_digest,
            "source_revision": source.source_revision,
            "data_format": source.data_format,
            "schema_version": source.schema_version,
            "size_bytes": source.size_bytes,
            "record_count": source.record_count,
        }
    assert "snapshot_ref" not in canonical_json(receipt)

    intent = EBMTrainingIntent(
        recipe={"id": "ebm_poc_replay_v1", "version": "1.0.0"},
        dataset={"id": "ebm-built", "version": "v1"},
        model={"architecture": "ebm_cnn_v1", "config": {"channels": [4, 8]}},
        training={"epochs": 1, "batch_size": 6, "pair_sample_size": 16},
    ).model_dump()
    spec = json.loads(
        ApprovedRecipeResolver(
            code_revision="test-code", image_digest="test-image", catalog=catalog
        ).resolve(canonical_json(intent), inputs.operator)
    )
    spec_digest = digest(["training-spec-v1", canonical_json(spec)])
    execution = tmp_path / "execution"
    execution.mkdir()
    (execution / "spec.json").write_text(
        json.dumps(
            {
                "spec": spec,
                "training_spec_digest": spec_digest,
                "producer": {"job_id": "job", "attempt_id": "attempt"},
            }
        )
    )
    environment = {
        **os.environ,
        "ML_DATASET_ROOT": str(components.settings.dataset_root),
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "bootstrap.ml_training_runner",
            "--directory",
            str(execution),
            "--cpu-seconds",
            "60",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert process.returncode == 0, process.stderr
    training_result = json.loads((execution / "result.json").read_text())
    manifest = json.loads((Path(training_result["bundle_path"]) / "manifest.json").read_text())
    assert manifest["lineage"]["dataset_digest"] == result.dataset.content_digest


def test_publication_is_idempotent_conflict_safe_and_never_registers_partial_versions(
    components, published_builder_inputs, tmp_path, monkeypatch
):
    inputs = published_builder_inputs
    service, catalog, publisher = builder_service(components, inputs, tmp_path)
    config = EBMDatasetBuilderConfig("recipe-a", 1)
    first = service.build(inputs.operator, "ebm-built", "v1", inputs.references, config)
    repeated = service.build(
        inputs.operator, "ebm-built", "v1", tuple(reversed(inputs.references)), config
    )
    assert repeated == first

    original_write_receipt = publisher._write_receipt

    def fail_receipt(_value):
        raise MLError("simulated receipt failure")

    monkeypatch.setattr(publisher, "_write_receipt", fail_receipt)
    with pytest.raises(MLError, match="simulated"):
        service.build(inputs.operator, "failed-build", "v1", inputs.references, config)
    with pytest.raises(NotFound):
        catalog.resolve(inputs.operator, "failed-build", "v1")
    monkeypatch.setattr(publisher, "_write_receipt", original_write_receipt)

    original_register = catalog.register_ebm

    def fail_registration(*_args, **_kwargs):
        raise MLError("simulated registration failure")

    monkeypatch.setattr(catalog, "register_ebm", fail_registration)
    with pytest.raises(MLError, match="registration failure"):
        service.build(inputs.operator, "failed-registration", "v1", inputs.references, config)
    with pytest.raises(NotFound):
        catalog.resolve(inputs.operator, "failed-registration", "v1")
    monkeypatch.setattr(catalog, "register_ebm", original_register)

    changed_records = copy.deepcopy(inputs.wafer_records)
    changed_records[0]["cdf_target"] = 0.91
    inputs.wafer_path.write_bytes(jsonl(changed_records))
    job = inputs.ingress.service.submit(
        inputs.operator, "builder-wafer-changed", IngestionRequest("builder-wafer", "v1")
    )
    assert inputs.ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    changed_snapshot = inputs.ingress.service.result(inputs.operator, job.job_id)
    changed_references = (
        inputs.references[0],
        EBMRawSnapshotReference(
            "wafer", changed_snapshot.snapshot_id, changed_snapshot.manifest_digest
        ),
    )
    with pytest.raises(Conflict, match="immutable"):
        service.build(inputs.operator, "ebm-built", "v1", changed_references, config)
    assert catalog.resolve(inputs.operator, "ebm-built", "v1") == first.dataset
