"""SQL-backed discovery, immutable publication, tenant/cursor isolation and rollout contracts."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from bootstrap.ml_gateway import create_app
from src.api.ml_schemas import PredictionRequest
from src.api.prediction_docs import PREDICT_EXAMPLES, GatewayPredictionResult
from src.application.prediction.catalog import ContractPublicationService, EndpointDiscoveryService
from src.domain.ml import Conflict, Forbidden, NotFound, SchemaMismatch, canonical_json, digest
from src.domain.prediction.catalog import CONTRACT_VERSION, PublicModelContract, contract_document
from src.infrastructure.persistence.ml_database import audit, routes
from src.infrastructure.persistence.prediction_catalog import SQLPredictionCatalog
from src.infrastructure.persistence.prediction_contract_schema import (
    public_contracts,
    usage_reviews,
)
from tests.ml.test_model_governance import evidence

USAGE = {
    "display_name": "Synthetic quality classifier",
    "purpose": "Classify two synthetic sensor values.",
    "applicable_conditions": "Synthetic engineering qualification.",
    "usage_notes": "Uncalibrated scores. Units are unspecified.",
    "support_info": "Contact the synthetic model owner.",
}


def technical(binding):
    return PublicModelContract(
        binding.binding_id,
        binding.manifest_digest,
        canonical_json(
            {
                "contract_version": CONTRACT_VERSION,
                "input_schema_version": "1",
                "output_schema_version": "1",
                "input_schema": PredictionRequest.model_json_schema(),
                "output_schema": GatewayPredictionResult.model_json_schema(),
                "request_example": PREDICT_EXAMPLES["mlp"]["value"],
                "limits": {"max_batch": 128, "max_body_bytes": 8 * 1024 * 1024},
                "preprocessing": {"strategy": "standard_scaler_v1"},
                "output_semantics": {"score_kind": "uncalibrated_softmax"},
                "registry_backend": "local",
                "input_units": {"sensor_a": None, "sensor_b": None},
            }
        ),
    )


def nested_technical_json(binding, depth):
    document = json.loads(technical(binding).document_json)
    document["output_semantics"] = {"value": "synthetic-nested-marker"}
    marker = '"synthetic-nested-marker"'
    return canonical_json(document).replace(marker, "[" * depth + marker + "]" * depth)


def publish(components, actor, binding):
    service = ContractPublicationService(components.registry, components.registry, technical)
    return service.publish(actor, binding.binding_id, USAGE)


def active(components, actor, intent, endpoint="quality", *, key="model", contract=True):
    binding, _, approval = evidence(components, actor, intent, key)
    if contract:
        publish(components, actor, binding)
    deployment = components.services.deployments.create(
        actor, endpoint, binding.binding_id, approval.approval_id
    )
    components.deployments.transition(deployment.deployment_id, "REQUESTED", "READY")
    components.deployments.activate(deployment)
    return binding, deployment


def catalog(components, *, clock=lambda: 1000.0):
    return EndpointDiscoveryService(
        SQLPredictionCatalog(components.database), "s" * 48, clock=clock
    )


def predictor(actor):
    return replace(actor, permissions=frozenset({"ml:predict"}))


@pytest.mark.parametrize(
    ("key", "value", "accepted"),
    [
        ("input_units", [], False),
        ("input_units", {"sensor": 123}, False),
        ("input_units", {"sensor": True}, False),
        ("input_units", {"sensor": None}, True),
        ("input_units", {"sensor": "V"}, True),
        ("output_semantics", [], False),
        ("limits", {"max_batch_size": 128.0}, False),
        ("limits", {"max_batch_size": True}, False),
        ("limits", {"max_batch_size": "128"}, False),
        ("limits", {"max_batch_size": 128}, True),
        ("input_schema_version", 1, False),
        ("output_schema_version", None, False),
        ("unknown", {}, False),
    ],
)
def test_technical_document_field_types_match_public_contract(key, value, accepted):
    from src.api.prediction_catalog import PublicTechnicalContract

    binding = SimpleNamespace(binding_id="synthetic-binding", manifest_digest="a" * 64)
    document = json.loads(technical(binding).document_json)
    document[key] = value
    raw = json.dumps(document)  # Retain integral floats for the strict type check.
    if accepted:
        assert (
            contract_document(raw) == PublicTechnicalContract.model_validate(document).model_dump()
        )
        canonical = canonical_json(document)
        contract = PublicModelContract(binding.binding_id, binding.manifest_digest, canonical)
        assert contract.document_json == canonical
        assert contract.contract_digest == digest(
            [CONTRACT_VERSION, binding.binding_id, binding.manifest_digest, document]
        )
    else:
        with pytest.raises(SchemaMismatch, match="Invalid public model contract"):
            contract_document(raw)


def test_technical_document_requires_all_fields():
    binding = SimpleNamespace(binding_id="synthetic-binding", manifest_digest="a" * 64)
    document = json.loads(technical(binding).document_json)
    del document["input_units"]
    with pytest.raises(SchemaMismatch, match="Invalid public model contract"):
        contract_document(canonical_json(document))


def test_invalid_technical_document_cannot_publish_an_immutable_contract(components, actor, intent):
    binding, _, _ = evidence(components, actor, intent)

    def invalid_verified_reader(binding):
        document = json.loads(technical(binding).document_json)
        document["limits"] = {"max_batch": True}
        return PublicModelContract(
            binding.binding_id, binding.manifest_digest, canonical_json(document)
        )

    service = ContractPublicationService(
        components.registry, components.registry, invalid_verified_reader
    )
    with pytest.raises(SchemaMismatch):
        service.publish(actor, binding.binding_id, USAGE)
    with components.database.transaction() as connection:
        assert connection.execute(select(public_contracts)).first() is None
        assert connection.execute(select(usage_reviews)).first() is None


def test_digest_valid_historical_schema_error_is_incomplete(components, actor, intent):
    binding, _ = active(components, actor, intent)
    document = json.loads(technical(binding).document_json)
    document["input_schema_version"] = 1
    with components.database.transaction() as connection:
        connection.execute(
            update(public_contracts)
            .where(public_contracts.c.binding_id == binding.binding_id)
            .values(
                document_json=canonical_json(document),
                contract_digest=digest(
                    [CONTRACT_VERSION, binding.binding_id, binding.manifest_digest, document]
                ),
            )
        )
    service = catalog(components)
    reader = predictor(actor)
    (summary,) = service.list(reader)["items"]
    assert summary["contract_status"] == "incomplete" and summary["input_schema_version"] is None
    detail = service.get(reader, "quality")
    assert detail["contract_status"] == "incomplete" and detail["contract"] is None
    assert detail["contract_digest"] is None and detail["request_example"] is None


def test_only_current_active_generation_is_visible(components, actor, intent):
    first, deployed = active(components, actor, intent)
    reader = predictor(actor)
    service = catalog(components)
    page = service.list(reader)
    assert [i["model_version"] for i in page["items"]] == [first.version]
    assert page["items"][0]["readiness"] == "unknown"
    pending, _, approval = evidence(components, actor, intent, "pending")
    candidate = components.services.deployments.create(
        actor, "quality", pending.binding_id, approval.approval_id
    )
    assert service.get(reader, "quality")["model_version"] == first.version
    publish(components, actor, pending)
    components.deployments.transition(candidate.deployment_id, "REQUESTED", "READY")
    components.deployments.activate(candidate)
    assert service.get(reader, "quality")["model_version"] == pending.version
    # Release the superseded slot through existing controller ownership rules.
    components.deployments.fence(deployed)
    components.deployments.release(deployed)
    components.deployments.transition(deployed.deployment_id, "RETIRING", "RETIRED")
    rollback = components.services.deployments.rollback(actor, deployed.deployment_id)
    components.deployments.transition(rollback.deployment_id, "REQUESTED", "READY")
    components.deployments.activate(rollback)
    result = service.get(reader, "quality")
    assert result["model_version"] == first.version
    assert result["generation"] > candidate.generation
    assert (
        EndpointDiscoveryService(
            SQLPredictionCatalog(components.database), "s" * 48, clock=lambda: 1000.0
        ).get(reader, "quality")
        == result
    )


def test_incomplete_legacy_contract_still_visible_and_backfilled(components, actor, intent):
    binding, _ = active(components, actor, intent, contract=False)
    service = catalog(components)
    reader = predictor(actor)
    detail = service.get(reader, "quality")
    assert detail["contract_status"] == "incomplete" and detail["contract"] is None
    publish(components, actor, binding)
    detail = service.get(reader, "quality")
    assert detail["contract_status"] == "complete"
    assert detail["request_example"]["endpoint_id"] == "quality"
    assert detail["contract"]["input_units"]["sensor_a"] is None
    assert "artifact_ref" not in json.dumps(detail)


def test_cursor_scope_expiry_and_tenant_visibility(components, actor, intent):
    active(components, actor, intent, "a")
    active(components, actor, intent, "b", key="b")
    service = catalog(components)
    reader = predictor(actor)
    first = service.list(reader, 1, cursor=None)
    assert first["items"][0]["endpoint_id"] == "a"
    cursor = first["next_cursor"]
    second = service.list(reader, 1, cursor)
    assert second["items"][0]["endpoint_id"] == "b" and second["next_cursor"] is None
    for other in (replace(reader, tenant_id="other"), replace(reader, actor_id="other")):
        with pytest.raises(SchemaMismatch):
            service.list(other, 1, cursor)
    for token, limit in ((cursor + "x", 1), ("bad", 1), (cursor, 2), ("", 1)):
        with pytest.raises(SchemaMismatch):
            service.list(reader, limit, token)
    with pytest.raises(SchemaMismatch):
        catalog(components, clock=lambda: 2000).list(reader, 1, cursor)
    assert service.list(replace(reader, tenant_id="other"))["items"] == []
    for who in (replace(reader, tenant_id="other"), reader):
        with pytest.raises(NotFound):
            service.get(who, "missing")
    with pytest.raises(Forbidden):
        service.list(replace(reader, permissions=frozenset({"ml:model:read"})))
    with pytest.raises(Forbidden):
        service.list(replace(reader, permissions=frozenset()), 1, cursor)


def test_immutable_technical_and_review_revision_audit(components, actor, intent):
    binding, _, _ = evidence(components, actor, intent)
    assert publish(components, actor, binding) == 1
    assert publish(components, actor, binding) == 1
    changed = copy.deepcopy(USAGE)
    changed["purpose"] = "Updated reviewed scope."
    service = ContractPublicationService(components.registry, components.registry, technical)
    assert service.publish(actor, binding.binding_id, changed) == 2
    assert service.publish(actor, binding.binding_id, changed) == 2
    original = technical(binding)
    mutated = json.loads(original.document_json)
    mutated["limits"]["max_batch"] = 16
    with pytest.raises(Conflict):
        components.registry.publish_contract(
            actor, replace(original, document_json=canonical_json(mutated)), canonical_json(USAGE)
        )
    with pytest.raises(Forbidden):
        service.publish(predictor(actor), binding.binding_id, USAGE)
    with pytest.raises(NotFound):
        service.publish(replace(actor, tenant_id="other"), binding.binding_id, USAGE)
    with pytest.raises(SchemaMismatch):
        service.publish(actor, binding.binding_id, {**USAGE, "credential": "forbidden"})
    with components.database.transaction() as c:
        reviews = list(c.execute(select(usage_reviews)))
        events = list(c.execute(select(audit.c.action)))
    assert len(reviews) == 2
    assert [r[0] for r in events].count("model.contract.publish") == 1
    assert [r[0] for r in events].count("model.usage.review") == 2


def publication_state(components):
    with components.database.transaction() as connection:
        return tuple(
            connection.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in (public_contracts, usage_reviews, audit)
        )


@pytest.mark.parametrize("existing_contract", [True, False])
@pytest.mark.parametrize("identical", [True, False])
def test_usage_review_rejects_inconsistent_tenant_atomically(
    components, actor, intent, existing_contract, identical
):
    binding, _ = active(components, actor, intent, contract=existing_contract)
    wrong_tenant = "synthetic-inconsistent-tenant"
    stored = canonical_json({**USAGE, "purpose": "Synthetic inconsistent review"})
    with components.database.transaction() as connection:
        connection.execute(
            insert(usage_reviews).values(
                binding_id=binding.binding_id,
                revision=8,
                tenant_id=wrong_tenant,
                review_json=stored,
                review_digest=digest([binding.binding_id, 8, stored]),
                reviewer_id="synthetic-reviewer",
                created_at=1.0,
            )
        )
    before = publication_state(components)
    usage = stored if identical else canonical_json(USAGE)
    with pytest.raises(Conflict, match="^Stored usage review identity mismatch$"):
        components.registry.publish_contract(actor, technical(binding), usage)
    assert publication_state(components) == before
    submitted_revision = 1 if existing_contract else None
    assert catalog(components).get(predictor(actor), "quality")["usage_revision"] == (
        submitted_revision
    )
    assert components.services.governance.contract(actor, binding.binding_id)["usage_revision"] == (
        submitted_revision
    )


def test_usage_review_revision_exhaustion_preserves_valid_replay(components, actor, intent):
    binding, _ = active(components, actor, intent)
    contract = technical(binding)
    maximum = 2_147_483_647
    original = canonical_json(USAGE)
    revised = canonical_json({**USAGE, "purpose": "Synthetic review at the limit"})
    with components.database.transaction() as connection:
        connection.execute(
            update(usage_reviews)
            .where(usage_reviews.c.binding_id == binding.binding_id)
            .values(
                revision=maximum - 1,
                review_digest=digest([binding.binding_id, maximum - 1, original]),
            )
        )
    assert components.registry.publish_contract(actor, contract, revised) == maximum
    before = publication_state(components)
    assert components.registry.publish_contract(actor, contract, revised) == maximum
    with pytest.raises(Conflict, match="^Usage review revision limit reached$"):
        components.registry.publish_contract(actor, contract, original)
    assert publication_state(components) == before
    assert catalog(components).get(predictor(actor), "quality")["usage_revision"] == maximum
    assert components.services.governance.contract(actor, binding.binding_id)["usage_revision"] == (
        maximum
    )


def test_usage_review_rejects_invalid_stored_revision_before_replay(components, actor, intent):
    binding, _ = active(components, actor, intent)
    raw = canonical_json(USAGE)
    contract = technical(binding)
    revisions = [0, -1]
    if components.database.engine.dialect.name == "sqlite":
        revisions += [2_147_483_648, 9_223_372_036_854_775_807]
    for revision in revisions:
        with components.database.transaction() as connection:
            connection.execute(
                update(usage_reviews)
                .where(usage_reviews.c.binding_id == binding.binding_id)
                .values(
                    revision=revision,
                    review_digest=digest([binding.binding_id, revision, raw]),
                )
            )
        before = publication_state(components)
        with pytest.raises(Conflict, match="^Stored usage review revision is invalid$"):
            components.registry.publish_contract(actor, contract, raw)
        assert publication_state(components) == before


@pytest.mark.parametrize("depth", [10_000, 600], ids=["decode", "canonicalize"])
def test_nested_technical_document_is_rejected_before_publication(components, actor, intent, depth):
    binding, _, _ = evidence(components, actor, intent)
    raw = nested_technical_json(binding, depth)
    assert len(raw.encode()) < 256 * 1024
    if depth == 600:
        document = json.loads(raw)
        with pytest.raises(RecursionError):
            canonical_json(document)
    else:
        with pytest.raises(RecursionError):
            json.loads(raw)
    before = publication_state(components)
    service = ContractPublicationService(
        components.registry,
        components.registry,
        lambda binding: PublicModelContract(binding.binding_id, binding.manifest_digest, raw),
    )
    with pytest.raises(SchemaMismatch, match="^Invalid public model contract$"):
        service.publish(actor, binding.binding_id, USAGE)
    assert publication_state(components) == before


@pytest.mark.parametrize("column", ["contract_digest", "manifest_digest"])
def test_corrupt_contract_is_incomplete_and_released_route_hidden(
    components, actor, intent, column
):
    binding, _ = active(components, actor, intent)
    service = catalog(components)
    with components.database.transaction() as c:
        c.execute(update(public_contracts).values(**{column: "0" * 64}))
    detail = service.get(predictor(actor), "quality")
    assert detail["contract"] is None and detail["contract_status"] == "incomplete"
    with pytest.raises(Conflict):
        publish(components, actor, binding)
    with components.database.transaction() as c:
        c.execute(update(routes).values(generation=999))
    assert service.list(predictor(actor))["items"] == []


def test_http_only_predictor_can_discover_contract_and_predict(components, actor, intent):
    from src.domain.deployments.identity import serving_identity

    _, deployed = active(components, actor, intent)
    reader = predictor(actor)

    async def resolve(token):
        if token == "revoked":
            from src.infrastructure.ml_identity import AuthenticationRequired

            raise AuthenticationRequired("Invalid credentials")
        return reader if token == "reader" else replace(reader, tenant_id="other")

    def handle(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    **serving_identity(deployed),
                    "ready": True,
                    "tenant_id": actor.tenant_id,
                    "pod_uid": "",
                },
            )
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "predictions": [{"class_id": 1, "label": "positive", "score": 0.7}],
                "provenance": {
                    **serving_identity(deployed),
                    "pod_uid": "",
                    "input_schema_version": body["schema_version"],
                },
                "request_id": request.headers["x-request-id"],
            },
        )

    app = create_app(
        components=components,
        identity=SimpleNamespace(resolve=resolve),
        resolver=SimpleNamespace(resolve=lambda _: "http://synthetic-serving"),
        transport=httpx.MockTransport(handle),
        endpoint_discovery=catalog(components),
    )
    with TestClient(app) as client:
        missing_auth = client.get("/api/v1/prediction/endpoints")
        assert missing_auth.status_code == 401
        assert missing_auth.headers["cache-control"] == "private, no-store"
        headers = {"Authorization": "Bearer reader"}
        page = client.get("/api/v1/prediction/endpoints", headers=headers)
        assert page.status_code == 200 and page.headers["cache-control"] == "private, no-store"
        detail = client.get(page.json()["items"][0]["detail_url"], headers=headers).json()
        prediction = client.post(
            detail["predict_url"], headers=headers, json=detail["request_example"]
        )
        assert prediction.status_code == 200
        assert prediction.json()["provenance"]["model_version"] == detail["model_version"]
        for params in ("limit=101", "cursor=invalid", "cursor=", "tenant_id=other"):
            assert (
                client.get("/api/v1/prediction/endpoints?" + params, headers=headers).status_code
                == 422
            )
        assert (
            client.get(
                "/api/v1/prediction/endpoints/quality", headers={"Authorization": "Bearer other"}
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/v1/prediction/endpoints", headers={"Authorization": "Bearer revoked"}
            ).status_code
            == 401
        )


def test_concurrent_registration_is_idempotent(components, actor, intent):
    from concurrent.futures import ThreadPoolExecutor

    binding, _, _ = evidence(components, actor, intent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, components, actor, binding) for _ in range(2)]
        assert [f.result(timeout=10) for f in futures] == [1, 1]
    with components.database.transaction() as c:
        assert len(list(c.execute(select(public_contracts)))) == 1
        assert len(list(c.execute(select(usage_reviews)))) == 1


def test_query_budget_is_one_bounded_select(components, actor, intent):
    from sqlalchemy import event

    active(components, actor, intent)
    seen = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(components.database.engine, "before_cursor_execute", observe)
    try:
        catalog(components).list(predictor(actor), 1)
    finally:
        event.remove(components.database.engine, "before_cursor_execute", observe)
    assert len(seen) == 1 and "LIMIT" in seen[0]


def test_additive_migration_preserves_old_authority(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    config = Config("alembic.ini")
    config.attributes["database_url"] = "sqlite:///" + str(tmp_path / "migration.db")
    command.upgrade(config, "0006_ingestion")
    engine = create_engine(config.attributes["database_url"])
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO ml_tenants (tenant_id, revision) VALUES ('old', 7)")
    command.upgrade(config, "head")
    assert {"model_public_contracts", "model_usage_reviews"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as c:
        assert (
            c.exec_driver_sql("SELECT revision FROM ml_tenants WHERE tenant_id='old'").scalar() == 7
        )
    command.downgrade(config, "0006_ingestion")
    assert "model_public_contracts" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    engine.dispose()


def test_concurrent_conflicting_technical_contract_has_one_winner(components, actor, intent):
    from concurrent.futures import ThreadPoolExecutor

    binding, _, _ = evidence(components, actor, intent)
    first = technical(binding)
    data = json.loads(first.document_json)
    data["limits"]["max_batch"] = 16
    second = replace(first, document_json=canonical_json(data))

    def submit(contract):
        try:
            components.registry.publish_contract(actor, contract, canonical_json(USAGE))
            return "created"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first, second)))
    assert sorted(results) == ["conflict", "created"]


def test_review_updates_do_not_change_technical_identity(components, actor, intent):
    binding, _ = active(components, actor, intent)
    service = catalog(components)
    before = service.get(predictor(actor), "quality")
    publication = ContractPublicationService(components.registry, components.registry, technical)
    revised = {**USAGE, "purpose": "New reviewed usage scope."}
    assert publication.publish(actor, binding.binding_id, revised) == 2
    after = service.get(predictor(actor), "quality")
    assert after["purpose"] == revised["purpose"] and after["usage_revision"] == 2
    assert after["contract_digest"] == before["contract_digest"]
    assert after["contract"] == json.loads(technical(binding).document_json)
    assert components.registry.get_model(actor, binding.binding_id) == binding


def test_publication_checks_exact_requested_binding(components, actor, intent):
    binding, _, _ = evidence(components, actor, intent)
    wrong = replace(technical(binding), manifest_digest="0" * 64)
    service = ContractPublicationService(components.registry, components.registry, lambda b: wrong)
    with pytest.raises(Conflict):
        service.publish(actor, binding.binding_id, USAGE)
    with components.database.transaction() as c:
        assert list(c.execute(select(public_contracts))) == []


@pytest.mark.parametrize(
    "value", [None, [], {"purpose": "incomplete"}, {**USAGE, "purpose": "<html>"}]
)
def test_review_input_is_bounded_and_allowlisted(value):
    from src.domain.prediction.catalog import reviewed_usage

    with pytest.raises(SchemaMismatch):
        reviewed_usage(value)


def test_operator_token_input_must_be_private_and_bounded(tmp_path):
    from bootstrap.prediction_contract_cli import bounded_file

    token = tmp_path / "synthetic-token"
    token.write_text("synthetic-session")
    token.chmod(0o600)
    assert bounded_file(token, 128, private=True) == b"synthetic-session"
    token.chmod(0o644)
    with pytest.raises(ValueError):
        bounded_file(token, 128, private=True)
    with pytest.raises(ValueError):
        bounded_file(token, 1)


@pytest.mark.parametrize("revision", [None, "", " ", "a" * 129])
def test_publication_requires_explicit_trusted_revision_before_artifact_access(revision):
    from bootstrap.prediction_contracts import extract_public_contract

    with pytest.raises(SchemaMismatch, match="qualified code revision"):
        extract_public_contract(None, None, expected_code_digest=revision)


def test_same_endpoint_name_is_scoped_to_each_tenant(components, actor, intent):
    first, _ = active(components, actor, intent)
    other = replace(actor, tenant_id="other-tenant")
    second, _ = active(components, other, intent, key="other-tenant-model")
    service = catalog(components)
    assert service.get(predictor(actor), "quality")["model_version"] == first.version
    assert service.get(predictor(other), "quality")["model_version"] == second.version
    assert [r["model_version"] for r in service.list(predictor(actor))["items"]] == [first.version]
    assert [r["model_version"] for r in service.list(predictor(other))["items"]] == [second.version]
