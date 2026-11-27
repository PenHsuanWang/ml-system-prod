"""Management governance discovery against tenant SQL and the existing state writers."""

from dataclasses import replace

import httpx
from sqlalchemy import update

from bootstrap.ml_api import create_app
from src.domain.ml import Actor, canonical_json
from src.domain.prediction.catalog import PublicModelContract
from src.infrastructure.persistence.ml_database import evaluations, jobs, routes
from src.infrastructure.persistence.prediction_contract_schema import public_contracts
from tests.ml.test_model_governance import evidence, ready
from tests.ml.test_prediction_catalog import publish

REGISTRY = "/api/v1/model_registry"
DEPLOYMENTS = "/api/v1/deployments"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def http(components, **actors):
    class TestIdentity:
        async def resolve(self, token):
            return actors[token]

    app = create_app(services=components.services, identity=TestIdentity())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://management")


async def test_binding_pagination_evaluation_and_approval_preserve_legacy(
    components, actor, intent
):
    first, evaluation, approval = evidence(components, actor, intent)
    second, _, _ = evidence(components, actor, intent, "next")
    foreign = replace(actor, tenant_id="foreign")
    evidence(components, foreign, intent, "foreign")
    reader = replace(actor, permissions=frozenset({"ml:model:read"}))
    with components.database.transaction() as connection:
        connection.execute(
            update(evaluations)
            .where(evaluations.c.id == evaluation.evaluation_id)
            .values(metrics_json='{"accuracy":0.9,"unknown":"PRIVATE_VALUE"}')
        )
    binding = f"{REGISTRY}/bindings/{first.binding_id}"
    headers = auth("reader")
    async with http(components, reader=reader, foreign=foreign) as client:
        assert (await client.get(f"{REGISTRY}/bindings")).status_code == 401
        legacy = (await client.get(f"{REGISTRY}/models", headers=headers)).json()
        assert isinstance(legacy, list) and {r["binding_id"] for r in legacy} == {
            first.binding_id,
            second.binding_id,
        }
        result = await client.get(f"{REGISTRY}/bindings", params={"limit": 1}, headers=headers)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "private, no-store"
        page = result.json()
        assert page["next_offset"] == 1
        later = (
            await client.get(
                f"{REGISTRY}/bindings", params={"offset": 1, "limit": 1}, headers=headers
            )
        ).json()
        assert later["next_offset"] is None
        assert [r["model_version"] for r in page["items"] + later["items"]] == sorted(
            (first.version, second.version)
        )
        family = await client.get(f"{REGISTRY}/models/{first.registered_name}", headers=headers)
        assert family.json() == {"registered_name": first.registered_name, "version_count": 2}
        exact = await client.get(
            f"{REGISTRY}/bindings", params={"model_version": first.version}, headers=headers
        )
        assert [r["binding_id"] for r in exact.json()["items"]] == [first.binding_id]
        assert (
            await client.get(f"{REGISTRY}/bindings?tenant_id=foreign", headers=headers)
        ).status_code == 422
        records = await client.get(f"{binding}/evaluation-results", headers=headers)
        assert records.status_code == 200, records.text
        assert records.json()["items"][0]["metrics"] == {"accuracy": 0.9}
        assert records.json()["items"][0]["evaluation_id"] == evaluation.evaluation_id
        assert "PRIVATE_VALUE" not in records.text
        approvals = await client.get(f"{binding}/approvals", headers=headers)
        assert approvals.status_code == 200, approvals.text
        record = approvals.json()["items"][0]
        assert record["approval_id"] == approval.approval_id and record["environment"] == "dev"
        assert record["report_digest"] == approval.report_digest and "approver_id" not in record
        for suffix in ("evaluation-results", "approvals", "contract", "lineage"):
            invisible = await client.get(f"{binding}/{suffix}", headers=auth("foreign"))
            assert invisible.status_code == 404
        legacy = (await client.get(f"{binding}/evaluations", headers=headers)).json()
        assert legacy[0]["evaluation_id"] == evaluation.evaluation_id


async def test_binding_contract_integrity_and_lineage_permissions(components, actor, intent):
    binding, _, _ = evidence(components, actor, intent)
    peer = replace(actor, actor_id="peer", permissions=frozenset({"ml:model:read", "ml:train"}))
    job_reader = replace(peer, permissions=frozenset({"ml:model:read", "ml:jobs:read_all"}))
    base = f"{REGISTRY}/bindings/{binding.binding_id}"
    async with http(components, owner=actor, peer=peer, job_reader=job_reader) as client:
        legacy = await client.get(f"{base}/contract", headers=auth("owner"))
        assert legacy.status_code == 200 and legacy.json()["status"] == "incomplete"
        assert legacy.json()["contract"] is None
        publish(components, actor, binding)
        current = await client.get(f"{base}/contract", headers=auth("peer"))
        assert current.status_code == 200, current.text
        assert current.json()["status"] == "complete" and current.json()["usage_revision"] == 1
        assert current.json()["contract_digest"]
        owned = await client.get(f"{base}/lineage", headers=auth("owner"))
        assert owned.status_code == 200 and owned.json()["status"] == "complete"
        assert owned.json()["training"]["attempt_id"] == binding.attempt_id
        assert owned.json()["dataset"]["id"] == "dataset-demo"
        assert owned.json()["run_id"] == binding.run_id
        assert binding.artifact_ref not in owned.text and "snapshot_ref" not in owned.text
        partial = (await client.get(f"{base}/lineage", headers=auth("job_reader"))).json()
        assert partial["status"] == "incomplete" and partial["training"] == owned.json()["training"]
        assert partial["dataset"] is None
        hidden = (await client.get(f"{base}/lineage", headers=auth("peer"))).json()
        assert hidden["training"] is None and hidden["dataset"] is None
        with components.database.transaction() as connection:
            connection.execute(
                update(public_contracts)
                .where(public_contracts.c.binding_id == binding.binding_id)
                .values(contract_digest="corrupt")
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == owned.json()["training"]["job_id"])
                .values(spec_json="{}")
            )
        invalid_contract = (await client.get(f"{base}/contract", headers=auth("owner"))).json()
        invalid_lineage = (await client.get(f"{base}/lineage", headers=auth("owner"))).json()
        assert invalid_contract["status"] == "incomplete" and invalid_contract["contract"] is None
        assert invalid_lineage["status"] == "incomplete" and invalid_lineage["training"] is None
        # A digest-valid historical document can still lack the public schema types.
        document = current.json()["contract"]
        document["input_units"] = []
        malformed = PublicModelContract(
            binding.binding_id, binding.manifest_digest, canonical_json(document)
        )
        with components.database.transaction() as connection:
            connection.execute(
                update(public_contracts)
                .where(public_contracts.c.binding_id == binding.binding_id)
                .values(
                    document_json=malformed.document_json, contract_digest=malformed.contract_digest
                )
            )
        response = await client.get(f"{base}/contract", headers=auth("owner"))
        assert response.status_code == 200 and response.json()["contract"] is None


async def test_deployment_read_scope_observes_rollout_and_rollback(components, actor, intent):
    existing, _, first_approval = evidence(components, actor, intent)
    replacement, _, next_approval = evidence(components, actor, intent, "replacement")
    first = components.services.deployments.create(
        actor, "quality", existing.binding_id, first_approval.approval_id
    )
    route = f"{DEPLOYMENTS}/endpoints/quality"
    read_only = Actor.from_identity(actor.tenant_id, "reader", frozenset({"ml.deployment.read"}))
    foreign = replace(read_only, tenant_id="foreign")
    predictor = replace(actor, permissions=frozenset({"ml:predict"}))
    headers = auth("reader")
    async with http(
        components, reader=read_only, writer=actor, foreign=foreign, predictor=predictor
    ) as client:
        initial = await client.get(route, headers=headers)
        assert initial.status_code == 200, initial.text
        assert initial.json()["route_state"] == "unassigned"
        ready(components, first)
        components.deployments.activate(first)
        second = components.services.deployments.create(
            actor, "quality", replacement.binding_id, next_approval.approval_id
        )
        pending = (await client.get(route, headers=headers)).json()
        assert pending["deployment_id"] == first.deployment_id and pending["readiness"] == "unknown"
        ready(components, second)
        components.deployments.activate(second)
        observed = await client.get(f"{DEPLOYMENTS}/endpoints", headers=auth("writer"))
        assert observed.status_code == 200, observed.text
        assert observed.json()["items"][0]["deployment_id"] == second.deployment_id
        assert observed.json()["items"][0]["model_version"] == replacement.version
        history = await client.get(f"{route}/history", params={"limit": 1}, headers=headers)
        assert history.status_code == 200, history.text
        assert history.json()["next_offset"] == 1 and history.json()["items"][0]["routed"]
        earlier = (
            await client.get(f"{route}/history", params={"offset": 1, "limit": 1}, headers=headers)
        ).json()["items"][0]
        assert earlier["deployment_id"] == first.deployment_id and not earlier["routed"]
        records = await client.get(
            DEPLOYMENTS,
            params={"state": "ACTIVE", "environment": "dev", "endpoint_id": "quality"},
            headers=headers,
        )
        assert records.status_code == 200, records.text
        assert [row["deployment_id"] for row in records.json()["items"]] == [second.deployment_id]
        first_url = f"{DEPLOYMENTS}/{first.deployment_id}"
        assert (await client.get(first_url, headers=headers)).status_code == 200
        events = await client.get(f"{first_url}/events", headers=headers)
        assert events.status_code == 200, events.text
        assert [row["action"] for row in events.json()["items"]] == ["deployment.create"]
        assert "actor_id" not in events.text
        for path in (route, f"{route}/history", f"{first_url}/events"):
            assert (await client.get(path, headers=auth("foreign"))).status_code == 404
        assert (await client.get(DEPLOYMENTS, headers=auth("foreign"))).json()["items"] == []
        assert (await client.get(DEPLOYMENTS, headers=auth("predictor"))).status_code == 403
        payload = {
            "endpoint_id": "other",
            "model_binding_id": existing.binding_id,
            "approval_id": first_approval.approval_id,
        }
        assert (await client.post(DEPLOYMENTS, json=payload, headers=headers)).status_code == 403
        assert (await client.post(f"{first_url}/rollback", headers=headers)).status_code == 403
        components.deployments.release(first)
        components.deployments.transition(first.deployment_id, "RETIRING", "RETIRED")
        rollback = await client.post(f"{first_url}/rollback", headers=auth("writer"))
        assert rollback.status_code == 202, rollback.text
        assert rollback.json()["generation"] == 3
        unchanged = (await client.get(route, headers=headers)).json()
        assert unchanged["deployment_id"] == second.deployment_id
        generation = components.deployments.get(actor, rollback.json()["deployment_id"])
        ready(components, generation)
        components.deployments.activate(generation)
        restored = (await client.get(route, headers=headers)).json()
        assert restored["generation"] == 3 and restored["model_version"] == existing.version
        assert restored["route_revision"] > unchanged["route_revision"]
        with components.database.transaction() as connection:
            connection.execute(
                update(routes).where(routes.c.endpoint_id == "quality").values(generation=999)
            )
        inconsistent = (await client.get(route, headers=headers)).json()
        assert (
            inconsistent["route_state"] == "inconsistent" and inconsistent["deployment_id"] is None
        )
        schema = (await client.get("/openapi.json")).json()
        assert schema["paths"][DEPLOYMENTS]["get"]["security"]
        assert schema["paths"][DEPLOYMENTS]["get"]["operationId"] == "list_deployments"
