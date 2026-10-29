import time
from dataclasses import asdict
from uuid import uuid4
from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.domain.ml import Actor, Conflict, NotFound
from src.domain.model_registry.models import Approval, Evaluation, ModelBinding
from src.infrastructure.persistence.ml_database import approvals, Database, evaluations, models
from src.infrastructure.persistence.ml_training import record_audit


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
