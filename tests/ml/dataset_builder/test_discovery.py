"""SQL/API discovery and atomic receipt publication through the existing builder."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select, update

from bootstrap.ml_api import create_app
from src.domain.datasets.ebm_builder import EBMDatasetBuilderConfig, EBMRawSnapshotReference
from src.domain.datasets.lineage import DatasetBuildReceipt
from src.domain.ingestion.discovery import IngestionCollectionQuery
from src.domain.ingestion.models import IngestionRequest
from src.domain.ml import Conflict, Forbidden, MLError, NotFound, canonical_json
from src.domain.pagination import CollectionWindow
from src.infrastructure.persistence.dataset_lineage_schema import dataset_lineage
from src.infrastructure.persistence.ml_database import audit, datasets
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from tests.ml.dataset_builder.test_integration import builder_service
from tests.ml.test_api import Identity
from tests.ml.test_datasets import snapshot_value


def build(components, inputs, tmp_path, *, dataset_id="built", version="v1"):
    service, catalog, publisher = builder_service(components, inputs, tmp_path)
    result = service.build(
        inputs.operator,
        dataset_id,
        version,
        inputs.references,
        EBMDatasetBuilderConfig("recipe-a", 1),
    )
    return result, service, catalog, publisher


def test_sql_lineage_is_atomic_idempotent_and_bound_to_exact_dataset(
    components,
    published_builder_inputs,
    tmp_path,
):
    inputs = published_builder_inputs
    result, service, catalog, _ = build(components, inputs, tmp_path)
    repeated = service.build(
        inputs.operator,
        "built",
        "v1",
        tuple(reversed(inputs.references)),
        EBMDatasetBuilderConfig("recipe-a", 1),
    )
    assert repeated == result
    lineage = components.services.datasets.lineage(inputs.operator, "built", "v1")
    assert lineage["status"] == "complete"
    assert (
        hashlib.sha256(canonical_json(lineage["receipt"]).encode()).hexdigest()
        == result.receipt_digest
    )
    assert lineage["receipt"]["dataset"]["content_digest"] == result.dataset.content_digest
    assert [i["role"] for i in lineage["receipt"]["inputs"]] == ["fdc", "wafer"]
    assert "snapshot_ref" not in canonical_json(lineage)
    with components.database.transaction() as c:
        assert len(list(c.execute(select(dataset_lineage)))) == 1
        assert (
            len(list(c.execute(select(audit).where(audit.c.action == "dataset.lineage.register"))))
            == 1
        )
    # Separate catalog/service instances retrieve the same durable version/provenance.
    assert catalog.lineage(inputs.operator, "built", "v1") == lineage


def test_lineage_respects_dataset_and_raw_job_visibility(
    components, published_builder_inputs, tmp_path
):
    inputs = published_builder_inputs
    build(components, inputs, tmp_path)
    service = components.services.datasets
    dataset_reader = replace(inputs.operator, permissions=frozenset({"ml:dataset:read"}))
    assert service.metadata(dataset_reader, "built", "v1")["lineage_status"] == "recorded"
    restricted = service.lineage(dataset_reader, "built", "v1")
    assert restricted["status"] == "restricted"
    assert restricted["receipt"] is None and inputs.fdc_snapshot.snapshot_id not in canonical_json(
        restricted
    )
    other_owner = replace(
        inputs.operator,
        actor_id="another-reader",
        permissions=frozenset({"ml:dataset:read", "ml:ingestion:read"}),
    )
    assert service.lineage(other_owner, "built", "v1")["status"] == "restricted"
    operator = replace(other_owner, permissions=other_owner.permissions | {"ml:ingestion:operate"})
    assert service.lineage(operator, "built", "v1")["status"] == "complete"
    with pytest.raises(NotFound):
        service.lineage(replace(operator, tenant_id="other"), "built", "v1")
    with pytest.raises(Forbidden):
        service.lineage(
            replace(inputs.operator, permissions=frozenset({"ml:dataset:manage"})), "built", "v1"
        )


def test_legacy_import_does_not_acquire_invented_history(
    components, published_builder_inputs, tmp_path
):
    inputs = published_builder_inputs
    result, service, catalog, _ = build(components, inputs, tmp_path)
    # Register an independent legacy version directly through the existing import contract.
    artifact = Path(result.dataset.snapshot_ref).parent
    catalog.register_ebm(
        inputs.operator, "legacy", "v1", artifact / "metadata.json", artifact / "spectrograms.npz"
    )
    assert catalog.lineage(inputs.operator, "legacy", "v1")["status"] == "incomplete"
    service.build(
        inputs.operator, "legacy", "v1", inputs.references, EBMDatasetBuilderConfig("recipe-a", 1)
    )
    assert catalog.lineage(inputs.operator, "legacy", "v1")["receipt"] is None
    assert (
        components.services.datasets.lineage(inputs.operator, "dataset-demo", "2026-10-28-v1")[
            "status"
        ]
        == "incomplete"
    )


@pytest.mark.parametrize("failure", ["filesystem", "sql", "stored"])
def test_invalid_receipts_fail_closed_without_partial_catalog(
    components,
    published_builder_inputs,
    tmp_path,
    monkeypatch,
    failure,
):
    inputs = published_builder_inputs
    service, catalog, publisher = builder_service(components, inputs, tmp_path)
    config = EBMDatasetBuilderConfig("recipe-a", 1)
    if failure == "filesystem":
        original = publisher._write_receipt

        def corrupt(value):
            checksum, ref = original(value)
            Path(ref).write_text("{}")
            return checksum, ref

        monkeypatch.setattr(publisher, "_write_receipt", corrupt)
        with pytest.raises(MLError):
            service.build(inputs.operator, "bad", "v1", inputs.references, config)
        with pytest.raises(NotFound):
            catalog.resolve(inputs.operator, "bad", "v1")
    elif failure == "sql":

        def fail(*args, **kwargs):
            raise Conflict("simulated SQL receipt failure")

        monkeypatch.setattr(catalog, "_persist_lineage", fail)
        with pytest.raises(Conflict):
            service.build(inputs.operator, "bad", "v1", inputs.references, config)
        with pytest.raises(NotFound):
            catalog.resolve(inputs.operator, "bad", "v1")
    else:
        service.build(inputs.operator, "built", "v1", inputs.references, config)
        with components.database.transaction() as c:
            c.execute(update(dataset_lineage).values(receipt_json="{}"))
        value = catalog.lineage(inputs.operator, "built", "v1")
        assert value["status"] == "incomplete" and value["reason"] == "receipt_integrity_failed"
        with pytest.raises(Conflict):
            service.build(inputs.operator, "built", "v1", inputs.references, config)


def test_same_content_with_different_raw_origin_conflicts(
    components, published_builder_inputs, tmp_path
):
    inputs = published_builder_inputs
    first, service, _, _ = build(components, inputs, tmp_path)
    new_job = inputs.ingress.service.submit(
        inputs.operator, "new-origin", IngestionRequest("builder-fdc", "v1")
    )
    assert inputs.ingress.supervisor().run_once(job_id=new_job.job_id).state == "PUBLISHED"
    new_snapshot = inputs.ingress.service.result(inputs.operator, new_job.job_id)
    assert new_snapshot.content_digest == inputs.fdc_snapshot.content_digest
    refs = (
        EBMRawSnapshotReference("fdc", new_snapshot.snapshot_id, new_snapshot.manifest_digest),
        inputs.references[1],
    )
    with pytest.raises(Conflict, match="immutable"):
        service.build(inputs.operator, "built", "v1", refs, EBMDatasetBuilderConfig("recipe-a", 1))
    assert (
        components.services.datasets.lineage(inputs.operator, "built", "v1")["receipt_digest"]
        == first.receipt_digest
    )


def test_concurrent_build_registration_keeps_one_receipt(
    components, published_builder_inputs, tmp_path
):
    inputs = published_builder_inputs

    def run():
        return build(components, inputs, tmp_path)[0]

    with ThreadPoolExecutor(2) as pool:
        outcomes = [f.result(timeout=20) for f in [pool.submit(run), pool.submit(run)]]
    assert outcomes[0] == outcomes[1]
    with components.database.transaction() as c:
        assert len(list(c.execute(select(dataset_lineage)))) == 1


def test_concurrent_conflicting_origins_preserve_one_atomic_winner(
    components, published_builder_inputs, tmp_path
):
    inputs = published_builder_inputs
    job = inputs.ingress.service.submit(
        inputs.operator, "concurrent-origin", IngestionRequest("builder-fdc", "v1")
    )
    assert inputs.ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    snapshot = inputs.ingress.service.result(inputs.operator, job.job_id)
    alternative = (
        EBMRawSnapshotReference("fdc", snapshot.snapshot_id, snapshot.manifest_digest),
        inputs.references[1],
    )

    def run(refs):
        service, _, _ = builder_service(components, inputs, tmp_path)
        try:
            return service.build(
                inputs.operator, "racing", "v1", refs, EBMDatasetBuilderConfig("recipe-a", 1)
            )
        except Conflict as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(run, [inputs.references, alternative]))
    assert sum(isinstance(outcome, Conflict) for outcome in outcomes) == 1
    winner = next(outcome for outcome in outcomes if not isinstance(outcome, Conflict))
    lineage = components.services.datasets.lineage(inputs.operator, "racing", "v1")
    assert lineage["receipt_digest"] == winner.receipt_digest
    with components.database.transaction() as c:
        assert len(list(c.execute(select(dataset_lineage)))) == 1


@pytest.mark.parametrize("invalid", ["contract", "extra", "role", "digest", "config", "tenant"])
def test_tampered_receipt_contract_is_rejected(
    components, published_builder_inputs, tmp_path, invalid
):
    result, _, catalog, _ = build(components, published_builder_inputs, tmp_path)
    original = catalog.lineage(published_builder_inputs.operator, "built", "v1")["receipt"]
    value = copy.deepcopy(original)
    if invalid == "contract":
        value["receipt_version"] = "unknown"
    elif invalid == "extra":
        value["inputs"][0]["snapshot_ref"] = "DO-NOT-EXPOSE"
    elif invalid == "role":
        value["inputs"][0]["role"] = "sql"
    elif invalid == "digest":
        value["dataset"]["content_digest"] = "invalid"
    elif invalid == "config":
        value["configuration"]["segment_id"] = True
    else:
        value["tenant_id"] = "bad/tenant"
    payload = canonical_json(value)
    with pytest.raises(MLError):
        DatasetBuildReceipt(payload, hashlib.sha256(payload.encode()).hexdigest())
    with pytest.raises(MLError):
        DatasetBuildReceipt(canonical_json(original), "0" * 64)
    assert (
        catalog.lineage(published_builder_inputs.operator, "built", "v1")["receipt_digest"]
        == result.receipt_digest
    )


def test_dataset_pagination_public_shadow_and_metadata(components, actor, tmp_path):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    operator = replace(actor, permissions=actor.permissions | {"ml:dataset:manage"})
    source = tmp_path / "input.json"
    source.write_text(canonical_json(snapshot_value()))
    for dataset_id, version in (("a", "v1"), ("a", "v2"), ("versions", "v1")):
        catalog.register(operator, dataset_id, version, source)
    service = components.services.datasets
    first = service.families(actor, CollectionWindow(limit=1))
    assert first["items"][0]["dataset_id"] == "a" and first["items"][0]["version_count"] == 2
    assert first["next_offset"] == 1
    assert service.versions(actor, "a", CollectionWindow(limit=1, offset=1))["next_offset"] is None
    assert service.versions(actor, "missing", CollectionWindow())["items"] == []
    assert service.metadata(actor, "a", "v1")["samples"]["training"] == 384
    assert service.families(replace(actor, tenant_id="other"), CollectionWindow())["items"] == [
        {"dataset_id": "dataset-demo", "version_count": 1, "last_published_at": 0.0}
    ]
    # A previously created tenant record shadows the same public identity in SQL.
    with components.database.transaction() as c:
        row = dict(
            c.execute(select(datasets).where(datasets.c.tenant_id == "__public__")).mappings().one()
        )
        row.update(id="private-shadow", tenant_id=actor.tenant_id)
        c.execute(insert(datasets).values(**row))
    versions = service.versions(actor, "dataset-demo", CollectionWindow())
    assert len(versions["items"]) == 1 and versions["items"][0]["visibility"] == "tenant"


def test_ingestion_collection_filters_and_ownership(components, published_builder_inputs):
    inputs = published_builder_inputs
    service = inputs.ingress.service
    query = IngestionCollectionQuery(
        CollectionWindow(limit=1), source_id="builder-fdc", state="PUBLISHED"
    )
    jobs, more = service.collections(inputs.operator, query)
    assert len(jobs) == 1 and not more and jobs[0].source_id == "builder-fdc"
    snapshots, more = service.collections(inputs.operator, query, snapshots=True)
    assert len(snapshots) == 1 and snapshots[0].snapshot_id == inputs.fdc_snapshot.snapshot_id
    denied_owner = replace(
        inputs.operator, actor_id="other", permissions=frozenset({"ml:ingestion:read"})
    )
    assert service.collections(denied_owner, IngestionCollectionQuery())[0] == []
    assert service.collections(
        replace(denied_owner, permissions=frozenset({"ml:ingestion:operate"})),
        IngestionCollectionQuery(),
    )[0]
    assert (
        service.collections(
            replace(inputs.operator, tenant_id="other"), IngestionCollectionQuery()
        )[0]
        == []
    )
    with pytest.raises(Forbidden):
        service.collections(
            replace(inputs.operator, permissions=frozenset({"ml:ingestion:cancel"})),
            IngestionCollectionQuery(),
        )
    late = IngestionCollectionQuery(CollectionWindow(created_after=10**12))
    assert service.collections(inputs.operator, late)[0] == []
    with pytest.raises(MLError):
        CollectionWindow(created_after=2, created_before=1)


@pytest.mark.asyncio
async def test_http_user_discovers_ingestion_build_and_lineage_without_filesystem_ids(
    components,
    published_builder_inputs,
    tmp_path,
):
    inputs = published_builder_inputs
    result, _, _, _ = build(components, inputs, tmp_path)
    app = create_app(
        services=components.services,
        identity=Identity(inputs.operator),
        ingestion_service=inputs.ingress.service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        auth = {"Authorization": "Bearer owner"}
        assert (await client.get("/api/v1/datasets")).status_code == 401
        jobs = await client.get("/api/v1/ingestion/jobs?state=PUBLISHED", headers=auth)
        assert jobs.status_code == 200 and len(jobs.json()["items"]) == 2
        assert jobs.headers["Cache-Control"] == "private, no-store"
        raw = await client.get("/api/v1/ingestion/snapshots?limit=1", headers=auth)
        assert raw.status_code == 200 and raw.json()["next_offset"] == 1
        assert "snapshot_ref" not in raw.text and "owner_token" not in jobs.text
        families = await client.get("/api/v1/datasets?dataset_id=built", headers=auth)
        dataset_id = families.json()["items"][0]["dataset_id"]
        versions = await client.get(f"/api/v1/datasets/{dataset_id}/versions", headers=auth)
        assert versions.status_code == 200
        version = versions.json()["items"][0]["version"]
        details = await client.get(
            f"/api/v1/datasets/{dataset_id}/versions/{version}", headers=auth
        )
        assert details.json()["content_digest"] == result.dataset.content_digest
        assert details.headers["Cache-Control"] == "private, no-store"
        lineage = await client.get(
            f"/api/v1/datasets/{dataset_id}/versions/{version}/lineage", headers=auth
        )
        assert lineage.status_code == 200 and lineage.json()["status"] == "complete"
        assert lineage.json()["receipt"]["inputs"][0]["snapshot_id"] in {
            i["snapshot_id"]
            for i in (await client.get("/api/v1/ingestion/snapshots", headers=auth)).json()["items"]
        }
        old = await client.get("/api/v1/datasets/versions/built@v1", headers=auth)
        assert (
            old.status_code == 200 and old.json()["content_digest"] == result.dataset.content_digest
        )
        for path in (
            "/api/v1/datasets?tenant_id=other",
            "/api/v1/ingestion/jobs?owner_id=other",
            "/api/v1/ingestion/jobs?state=unknown",
            "/api/v1/ingestion/snapshots?state=PUBLISHED",
            "/api/v1/datasets?limit=101",
            "/api/v1/datasets?created_after=2&created_before=1",
            "/api/v1/ingestion/jobs?created_after=nan",
        ):
            assert (await client.get(path, headers=auth)).status_code == 422
        assert (
            await client.get(
                "/api/v1/datasets/built/versions/v1", headers={"Authorization": "Bearer other"}
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_metadata_http_permissions_errors_and_public_contracts(
    components, published_builder_inputs
):
    inputs = published_builder_inputs
    anonymous_rights = replace(inputs.operator, permissions=frozenset())
    app = create_app(
        services=components.services,
        identity=Identity(anonymous_rights),
        ingestion_service=inputs.ingress.service,
    )
    paths = (
        "/api/v1/datasets",
        "/api/v1/datasets/unknown/versions",
        "/api/v1/datasets/unknown/versions/v1",
        "/api/v1/datasets/unknown/versions/v1/lineage",
        "/api/v1/ingestion/jobs",
        "/api/v1/ingestion/snapshots",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in paths:
            assert (await client.get(path)).status_code == 401
            denied = await client.get(path, headers={"Authorization": "Bearer owner"})
            assert denied.status_code == 403 and not denied.json()["retryable"]
        schema = (await client.get("/openapi.json")).json()
        for path in paths:
            contract_path = path.replace("unknown", "{dataset_id}").replace(
                "/v1/lineage", "/{version}/lineage"
            )
            if contract_path.endswith("/versions/v1"):
                contract_path = contract_path[:-2] + "{version}"
            operation = schema["paths"][contract_path]["get"]
            assert {"200", "401", "403", "422", "503"} <= set(operation["responses"])
            assert operation["operationId"] and operation["description"]
        # Missing composition remains a typed 503 rather than an accidental attribute failure.
        app.state.ml_identity = Identity(inputs.operator)
        components.services.datasets.discovery = None
        unavailable = await client.get(paths[0], headers={"Authorization": "Bearer owner"})
        assert unavailable.status_code == 503 and unavailable.json()["retryable"]


@pytest.mark.asyncio
async def test_dataset_named_versions_preserves_legacy_route_resolution(
    components, actor, tmp_path
):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    source = tmp_path / "source.json"
    source.write_text(canonical_json(snapshot_value()))
    catalog.register(
        replace(actor, permissions=actor.permissions | {"ml:dataset:manage"}),
        "versions",
        "v1",
        source,
    )
    app = create_app(services=components.services, identity=Identity(actor))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        headers = {"Authorization": "Bearer owner"}
        response = await c.get("/api/v1/datasets/versions/versions", headers=headers)
        assert response.status_code == 200 and response.json()["items"][0]["version"] == "v1"
        assert (
            await c.get("/api/v1/datasets/versions/versions@v1", headers=headers)
        ).status_code == 200
