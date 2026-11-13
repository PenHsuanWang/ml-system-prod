"""Synthetic engineering evidence with real Worker, MLflow, Controller and HTTP Gateway.

The opt-in case uses disposable stack identity accounts, not identity doubles.
Its report always leaves approved enterprise-data qualification pending.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from bootstrap.ebm_e2e import verify_engineering_lifecycle
from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from src.domain.datasets.ebm_builder import EBMRawSnapshotReference
from src.domain.ingestion.models import IngestionRequest, SourceDefinition
from src.domain.ml import Actor, Conflict, MLError, NotFound, canonical_json
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.serving.materializer import verify_bundle
from tests.ml.dataset_builder.conftest import jsonl, raw_records
from tests.ml.ebm.test_platform_e2e import submit_ebm
from tests.ml.integration.test_backend_stack import wait_for_ready


def publish_inputs(components, actor, root):
    root.mkdir(parents=True)
    ingress = build_ingestion(
        IngestionSettings(
            database_url=components.settings.database_url,
            raw_root=root / "raw",
            file_roots=(("engineering", root),),
        ),
        database=components.database,
    )
    references = []
    try:
        for role, records in zip(("fdc", "wafer"), raw_records(lot_count=10), strict=True):
            if role == "wafer":
                for record in records:
                    record["cdf_target"] = float(record["cdf_target"]) / 2
            else:
                for record in records:
                    amplitude = 1 + int(record["lot_id"].rsplit("-", 1)[1]) / 5
                    for observation in record["observations"]:
                        observation["value"] *= amplitude
            (root / (role + ".jsonl")).write_bytes(jsonl(records))
            source = SourceDefinition(
                "engineering-" + role + "-" + uuid4().hex[:8],
                "v1",
                actor.tenant_id,
                "file",
                "1",
                canonical_json({"root_key": "engineering", "relative_path": role + ".jsonl"}),
            )
            ingress.sources.register(actor, source)
            job = ingress.service.submit(
                actor, uuid4().hex, IngestionRequest(source.source_id, "v1")
            )
            assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
            snapshot = ingress.service.result(actor, job.job_id)
            references.append(
                EBMRawSnapshotReference(role, snapshot.snapshot_id, snapshot.manifest_digest)
            )
    finally:
        ingress.close()
    return tuple(references)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_raw_snapshots_to_real_worker_mlflow_controller_and_http_gateway(tmp_path):
    management = os.environ.get("ML_TEST_MANAGEMENT_URL")
    prediction = os.environ.get("ML_TEST_PREDICTION_URL")
    if not management or not prediction:
        if os.environ.get("ML_REQUIRE_STACK_ACCEPTANCE") == "1":
            pytest.fail("Isolated EBM stack endpoints are required")
        pytest.skip("Requires the disposable backend stack")
    settings = MLSettings.from_env()
    assert settings.tracker_backend == "mlflow"
    tenant = "ebm-e2e-" + uuid4().hex[:12]
    actor = Actor(
        tenant,
        "engineering_reviewer",
        frozenset(
            {
                "ml:train",
                "ml:model:read",
                "ml:model:approve",
                "ml:deploy",
                "ml:predict",
                "ml:dataset:manage",
                "ml:source:manage",
                "ml:ingest",
                "ml:ingestion:operate",
            }
        ),
    )
    password = secrets.token_urlsafe(24)
    command = [
        "python",
        "manage.py",
        "create-admin",
        "--tenant-id",
        tenant,
        "--username",
        actor.actor_id,
        "--password-stdin",
    ]
    for permission in ("ml.train", "ml.model.read", "ml.model.approve", "ml.deploy", "ml.predict"):
        command.extend(["--permission", permission])
    provisioned = subprocess.run(
        command, input=password + "\n", text=True, capture_output=True, timeout=40, check=False
    )
    assert provisioned.returncode == 0, "Disposable reviewer provisioning failed"
    components = build_components(settings)
    report = {
        "report_version": "ebm-e2e-verification-v1",
        "source_commit_sha": os.environ["ML_E2E_SOURCE_COMMIT"],
        "data_kind": "synthetic-engineering-fixture",
        "status": "ENGINEERING_E2E_FAILED",
        "real_data_status": "EXTERNAL_QUALIFICATION_PENDING",
        "external_requirements": [
            "Approved de-identified enterprise ebm-raw-input-v1 snapshots",
            "Model-owner-approved quality policy",
            "Authorized human production approval",
        ],
        "test_execution": {"passed": 0, "failed": 0, "skipped": 0},
    }
    output = Path(os.environ.get("ML_EBM_E2E_REPORT", str(tmp_path / "ebm-e2e.json")))
    try:
        # Shared var namespace is visible to the real child and Controller.
        root = settings.execution_root / "engineering-inputs" / tenant
        references = publish_inputs(components, actor, root)
        async with httpx.AsyncClient(base_url=management, timeout=20) as manager:
            await wait_for_ready(manager)
            login = await manager.post(
                "/api/v1/identity/login",
                json={
                    "tenant_id": tenant,
                    "username": actor.actor_id,
                    "password": password,
                },
            )
            assert login.status_code == 200
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            manager.headers.update(headers)
            me = await manager.get("/api/v1/identity/me")
            assert me.status_code == 200
            actor = replace(actor, actor_id=me.json()["user_id"])
            async with httpx.AsyncClient(
                base_url=prediction, headers=headers, timeout=30
            ) as gateway:
                await wait_for_ready(gateway)
                await verify_engineering_lifecycle(
                    components,
                    actor,
                    references,
                    root / "raw",
                    manager,
                    gateway,
                    report,
                    dataset_id="ebm-" + uuid4().hex[:12],
                    endpoint="ebm-" + uuid4().hex[:12],
                )
        report["status"] = "ENGINEERING_E2E_PASSED"
        report["test_execution"]["passed"] = 1
    except Exception:
        report["test_execution"]["failed"] = 1
        report["failure_code"] = "ENGINEERING_LIFECYCLE_CHECK_FAILED"
        raise
    finally:
        components.database.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


@pytest.mark.torch
def test_rejected_policy_missing_evidence_and_wrong_digests_cannot_be_approved(
    components,
    actor,
    tmp_path,
):
    """Fast real-child governance regression; not full-stack qualification."""
    registered, job, command = submit_ebm(components, actor, tmp_path, "e2e-rejected")
    components.supervisor().execute(command)
    completed = components.training.get(job.job_id)
    assert completed.state == "SUCCEEDED"
    binding = components.registry.get_model(actor, completed.model_binding_id)
    (evaluation,) = components.registry.list_evaluations(binding.binding_id)
    assert evaluation.decision == "REJECTED_BY_POLICY"
    for evaluation_id in (evaluation.evaluation_id, "missing-evidence"):
        with pytest.raises((Conflict, NotFound)):
            components.services.registry.approve(actor, binding.binding_id, evaluation_id, "dev")
    with pytest.raises(NotFound):
        components.services.deployments.create(actor, "blocked-ebm", binding.binding_id, "missing")
    with pytest.raises(MLError):
        verify_bundle(Path(binding.artifact_ref), "0" * 64)
    with pytest.raises(MLError):
        EBMFileDatasetSource(components.settings.dataset_root).load(
            {
                "snapshot_ref": registered.snapshot_ref,
                "content_digest": "0" * 64,
            }
        )
    mismatched_spec = json.loads(job.spec_json)
    mismatched_spec["dataset"]["content_digest"] = "0" * 64
    with pytest.raises(MLError):
        components.supervisor().evaluator.evaluate(binding, canonical_json(mismatched_spec))
    with pytest.raises(Conflict):
        components.registry.save_evaluation(
            replace(evaluation, evaluation_id=uuid4().hex, manifest_digest="0" * 64)
        )
