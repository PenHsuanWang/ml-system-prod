"""Training discovery over the real Management, SQL and execution contracts."""

from dataclasses import replace

import httpx
import pytest
from sqlalchemy import update

from bootstrap.ml_api import create_app
from src.infrastructure.persistence.ml_database import attempts, jobs
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository
from tests.ml.ebm.test_platform_training import register
from tests.ml.test_api import Identity

TRAINING = "/api/v1/training"
AUTH = {"Authorization": "Bearer owner"}


@pytest.mark.parametrize("recipe_id", ["nn_classifier", "ebm_poc_replay_v1"])
async def test_discovered_recipe_can_submit_and_train(components, actor, tmp_path, recipe_id):
    if recipe_id == "ebm_poc_replay_v1":
        register(
            SQLDatasetCatalog(components.database, components.settings.dataset_root),
            actor,
            tmp_path,
        )
    app = create_app(services=components.services, identity=Identity(actor))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://management", headers=AUTH
    ) as client:
        listing = await client.get(f"{TRAINING}/recipes")
        assert listing.status_code == 200
        selected = next(item for item in listing.json() if item["recipe"]["id"] == recipe_id)
        version = selected["recipe"]["version"]
        detail = await client.get(f"{TRAINING}/recipes/{recipe_id}/versions/{version}")
        assert detail.status_code == 200
        recipe = detail.json()
        assert recipe["spec_schema"]["properties"]["training"]["$ref"]
        profiles = (await client.get(f"{TRAINING}/resource-profiles")).json()
        assert any(
            item["resource_profile_id"] == recipe["resource_profile_id"]
            and selected["recipe"] in item["recipes"]
            for item in profiles
        )
        example = recipe["submit_example"]
        dataset = example["spec"]["dataset"]
        admitted_dataset = await client.get(
            f"/api/v1/datasets/{dataset['id']}/versions/{dataset['version']}"
        )
        assert admitted_dataset.status_code == 200
        submit = await client.post(
            f"{TRAINING}/jobs", json=example, headers={"Idempotency-Key": "discovered"}
        )
        assert submit.status_code == 202, submit.text
        job_id = submit.json()["job_id"]
        job_url = f"{TRAINING}/jobs/{job_id}"
        pending = await client.get(f"{job_url}/metrics")
        assert pending.status_code == 200
        assert pending.json() == {
            "job_id": job_id,
            "attempt_id": submit.json()["attempt_id"],
            "status": "unknown",
            "samples": [],
        }
        spec = await client.get(f"{job_url}/spec")
        assert spec.status_code == 200, spec.text
        assert spec.json()["recipe"] == selected["recipe"]
        assert spec.json()["evaluation_policy"] == recipe["evaluation_policy"]
        assert spec.json()["dataset"]["content_digest"] == admitted_dataset.json()["content_digest"]
        assert "snapshot_ref" not in spec.text and str(tmp_path) not in spec.text
        command = SQLOutboxRepository(components.database).lease(1)[0].command
        components.supervisor().execute(command)
        listed = (await client.get(f"{TRAINING}/jobs", params={"state": "SUCCEEDED"})).json()
        assert len(listed["items"]) == 1
        job = listed["items"][0]
        assert job["job_id"] == job_id and job["model_binding_id"]
        assert job["spec_digest"] == spec.json()["spec_digest"]
        assert job["phase"] == "COMPLETED"
        metrics = await client.get(f"{job_url}/metrics")
        assert metrics.status_code == 200, metrics.text
        series = metrics.json()
        assert series["status"] == "recorded" and series["attempt_id"] == job["attempt_id"]
        expected_epochs = example["spec"]["training"]["epochs"]
        assert [sample["step"] for sample in series["samples"]] == list(range(expected_epochs))
        assert "loss" in series["samples"][0]["values"]
        assert (
            "validation_accuracy" if recipe_id == "nn_classifier" else "reconstruction_loss"
        ) in series["samples"][0]["values"]
        assert "bundle_ref" not in metrics.text and str(tmp_path) not in metrics.text


async def test_sql_job_pagination_and_detail_apply_existing_visibility(components, actor, intent):
    owner = replace(actor, permissions=frozenset())
    peer = replace(actor, actor_id="peer")
    reader = replace(peer, permissions=frozenset({"ml:jobs:read_all"}))
    foreign = replace(reader, tenant_id="other")
    actors = {"owner": owner, "peer": peer, "reader": reader, "foreign": foreign}

    class TestIdentity:
        async def resolve(self, token):
            return actors[token]

    first = components.services.training.submit(actor, "first", intent)
    second = components.services.training.submit(actor, "second", intent)
    third = components.services.training.submit(peer, "third", intent)
    components.services.training.cancel(actor, first.job_id)
    app = create_app(services=components.services, identity=TestIdentity())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://management"
    ) as client:
        assert (await client.get(f"{TRAINING}/jobs")).status_code == 401
        assert (await client.get(f"{TRAINING}/recipes", headers=AUTH)).status_code == 403
        assert (await client.get(f"{TRAINING}/resource-profiles", headers=AUTH)).status_code == 403
        assert (
            await client.get(
                f"{TRAINING}/recipes/missing/versions/v1", headers={"Authorization": "Bearer peer"}
            )
        ).status_code == 404
        page = await client.get(f"{TRAINING}/jobs", params={"limit": 1}, headers=AUTH)
        assert page.status_code == 200 and page.json()["next_offset"] == 1
        assert page.json()["items"][0]["job_id"] == second.job_id
        continuation = (
            await client.get(f"{TRAINING}/jobs", params={"offset": 1, "limit": 1}, headers=AUTH)
        ).json()
        assert continuation["items"][0]["job_id"] == first.job_id
        assert continuation["next_offset"] is None
        headers = {"Authorization": "Bearer reader"}
        all_jobs = (await client.get(f"{TRAINING}/jobs", headers=headers)).json()
        assert {job["job_id"] for job in all_jobs["items"]} == {
            first.job_id,
            second.job_id,
            third.job_id,
        }
        filtered = (
            await client.get(f"{TRAINING}/jobs", params={"state": "CANCELLED"}, headers=headers)
        ).json()
        assert [job["job_id"] for job in filtered["items"]] == [first.job_id]
        time_filtered = (
            await client.get(
                f"{TRAINING}/jobs", params={"created_after": first.created_at}, headers=AUTH
            )
        ).json()
        assert [job["job_id"] for job in time_filtered["items"]] == [second.job_id]
        assert (await client.get(f"{TRAINING}/jobs?state=bad", headers=AUTH)).status_code == 422
        assert (
            await client.get(f"{TRAINING}/jobs?tenant_id=other", headers=AUTH)
        ).status_code == 422
        assert (
            await client.get(f"{TRAINING}/jobs", headers={"Authorization": "Bearer foreign"})
        ).json()["items"] == []
        for path in ("spec", "metrics"):
            url = f"{TRAINING}/jobs/{first.job_id}/{path}"
            assert (await client.get(url, headers=headers)).status_code == 200
            for token in ("peer", "foreign"):
                assert (
                    await client.get(url, headers={"Authorization": f"Bearer {token}"})
                ).status_code == 404
        schema = (await client.get("/openapi.json")).json()
        operation = schema["paths"][f"{TRAINING}/jobs"]["get"]
        assert operation["security"] and operation["operationId"] == "list_training_jobs"


@pytest.mark.parametrize("resource", ["spec", "metrics"])
async def test_invalid_stored_training_metadata_returns_safe_unavailable(
    components, actor, intent, resource
):
    job = components.services.training.submit(actor, "evidence", intent)
    with components.database.transaction() as connection:
        if resource == "spec":
            connection.execute(update(jobs).where(jobs.c.id == job.job_id).values(spec_json="{}"))
        else:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == job.attempt_id)
                .values(result_json='{"private":"DO-NOT-SHOW"}')
            )
    app = create_app(services=components.services, identity=Identity(actor))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://management", headers=AUTH
    ) as client:
        response = await client.get(f"{TRAINING}/jobs/{job.job_id}/{resource}")
        assert response.status_code == 503
        assert "DO-NOT-SHOW" not in response.text
