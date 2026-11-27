import time
from dataclasses import asdict
from uuid import uuid4
from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.domain.ml import Actor, Conflict, NotFound
from src.domain.model_registry.models import Approval, Evaluation, ModelBinding
from src.domain.prediction.catalog import PublicModelContract
from src.infrastructure.persistence.ml_database import approvals, Database, evaluations, models
from src.infrastructure.persistence.ml_training import record_audit
from src.infrastructure.persistence.prediction_contract_schema import public_contracts, usage_reviews


def binding_from_row(row: Mapping[str, Any]) -> ModelBinding:
    values = dict(row)
    values["binding_id"] = values.pop("id")
    return ModelBinding(**values)


def evaluation_from_row(row: Mapping[str, Any]) -> Evaluation:
    values = dict(row)
    values["evaluation_id"] = values.pop("id")
    values.pop("created_at")
    return Evaluation(**values)


class SQLRegistryRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def save_binding(self, binding: ModelBinding) -> ModelBinding:
        try:
            with self.db.transaction() as c:
                existing = c.execute(select(models).where(
                    models.c.attempt_id == binding.attempt_id)).mappings().first()
                if existing is not None:
                    result = binding_from_row(existing)
                    if result.manifest_digest != binding.manifest_digest:
                        raise Conflict("Attempt already bound to different model content")
                    return result
                values = asdict(binding)
                values["id"] = values.pop("binding_id")
                c.execute(insert(models).values(**values))
            return binding
        except IntegrityError:
            raise Conflict("Model binding was concurrently registered") from None

    def get_model(self, actor: Actor, binding_id: str) -> ModelBinding:
        with self.db.transaction() as c:
            row = c.execute(select(models).where(models.c.id == binding_id,
                            models.c.tenant_id == actor.tenant_id)).mappings().first()
            if row is None:
                raise NotFound("Model not found")
            return binding_from_row(row)

    def list_models(self, actor: Actor) -> tuple[ModelBinding, ...]:
        with self.db.transaction() as c:
            return tuple(binding_from_row(r) for r in c.execute(select(models).where(
                models.c.tenant_id == actor.tenant_id)).mappings())

    def save_evaluation(self, evaluation: Evaluation) -> Evaluation:
        with self.db.transaction() as c:
            binding = c.execute(select(models).where(
                models.c.id == evaluation.model_binding_id)).mappings().one()
            if binding["manifest_digest"] != evaluation.manifest_digest:
                raise Conflict("Evaluation artifact does not match model")
            values = asdict(evaluation)
            values["id"] = values.pop("evaluation_id")
            c.execute(insert(evaluations).values(**values, created_at=time.time()))
        return evaluation

    def get_evaluation(self, evaluation_id: str) -> Evaluation:
        with self.db.transaction() as c:
            row = c.execute(select(evaluations).where(
                evaluations.c.id == evaluation_id)).mappings().first()
            if row is None:
                raise NotFound("Evaluation not found")
            return evaluation_from_row(row)

    def list_evaluations(self, binding_id: str) -> tuple[Evaluation, ...]:
        with self.db.transaction() as c:
            return tuple(evaluation_from_row(r) for r in c.execute(select(evaluations).where(
                evaluations.c.model_binding_id == binding_id).order_by(evaluations.c.created_at)).mappings())

    def approve(self, actor: Actor, binding: ModelBinding, evaluation: Evaluation,
                environment: str) -> Approval:
        approval = Approval(str(uuid4()), actor.tenant_id, binding.binding_id,
                            evaluation.evaluation_id, evaluation.report_digest, binding.binding_digest,
                            environment, actor.actor_id)
        if evaluation.model_binding_id != binding.binding_id or (
            evaluation.manifest_digest != binding.manifest_digest
        ) or evaluation.decision != "APPROVED_BY_POLICY":
            raise Conflict("Approval evidence mismatch")
        with self.db.transaction() as c:
            values = asdict(approval)
            values["id"] = values.pop("approval_id")
            c.execute(insert(approvals).values(**values, created_at=time.time()))
            record_audit(c, actor, "model.approve", approval.approval_id)
        return approval

    def get_approval(self, actor: Actor, approval_id: str) -> Approval:
        with self.db.transaction() as c:
            row = c.execute(select(approvals).where(approvals.c.id == approval_id,
                                                    approvals.c.tenant_id == actor.tenant_id)).mappings().first()
            if row is None:
                raise NotFound("Approval not found")
            values = dict(row)
            values["approval_id"] = values.pop("id")
            values.pop("created_at")
            return Approval(**values)


    def publish_contract(self, actor: Actor, contract: "PublicModelContract", usage_json: str) -> int:
        from src.domain.ml import digest
        from src.domain.prediction.catalog import reviewed_usage
        from src.infrastructure.persistence.ml_training import lock_tenant
        import json

        actor.require("ml:model:approve")
        actor.require("ml:model:read")
        # Validate the separately reviewed allowlist even when called by an operator tool.
        if reviewed_usage(json.loads(usage_json)) != usage_json:
            raise Conflict("Usage review must be canonical")
        with self.db.transaction() as c:
            lock_tenant(c, actor.tenant_id)
            binding = c.execute(select(models).where(
                models.c.id == contract.binding_id,
                models.c.tenant_id == actor.tenant_id,
            )).mappings().first()
            if binding is None:
                raise NotFound("Model not found")
            if binding["manifest_digest"] != contract.manifest_digest:
                raise Conflict("Public contract does not match the model manifest")
            current = c.execute(select(public_contracts).where(
                public_contracts.c.binding_id == contract.binding_id)).mappings().first()
            if current is not None:
                if (current["manifest_digest"] != contract.manifest_digest
                        or current["contract_digest"] != contract.contract_digest
                        or current["document_json"] != contract.document_json
                        or current["tenant_id"] != actor.tenant_id):
                    raise Conflict("Published technical contracts are immutable")
            else:
                c.execute(insert(public_contracts).values(
                    binding_id=contract.binding_id, tenant_id=actor.tenant_id,
                    manifest_digest=contract.manifest_digest,
                    contract_digest=contract.contract_digest, document_json=contract.document_json,
                    created_at=time.time()))
                record_audit(c, actor, "model.contract.publish", contract.contract_digest)
            review = c.execute(select(usage_reviews).where(
                usage_reviews.c.binding_id == contract.binding_id).order_by(
                usage_reviews.c.revision.desc()).limit(1)).mappings().first()
            if review is not None and review["review_json"] == usage_json:
                return int(review["revision"])
            revision = 1 if review is None else int(review["revision"]) + 1
            review_digest = digest([contract.binding_id, revision, usage_json])
            c.execute(insert(usage_reviews).values(
                binding_id=contract.binding_id, revision=revision, tenant_id=actor.tenant_id,
                review_json=usage_json, review_digest=review_digest,
                reviewer_id=actor.actor_id, created_at=time.time()))
            record_audit(c, actor, "model.usage.review", review_digest)
            return revision
