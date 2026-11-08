"""Monitoring observes durable state with a fixed, non-business label vocabulary."""

import time

from sqlalchemy import update

from src.domain.ingestion.models import IngestionRequest
from src.infrastructure.ingestion.persistence.metrics import SQLIngestionMetricsReader
from src.infrastructure.ingestion.persistence.schema import ingestion_jobs
from tests.ml.ingestion.test_service import ingress_environment


def test_ingestion_metrics_count_jobs_without_tenant_source_or_job_labels(
    components, actor, tmp_path
):
    ingress, _, owner, _, _ = ingress_environment(components, actor, tmp_path)
    job = ingress.service.submit(owner, "metric-test", IngestionRequest("events", "v1"))
    reader = SQLIngestionMetricsReader(ingress.database)
    text = reader.render(now=job.created_at + 5)
    assert 'ml_ingestion_jobs{state="QUEUED"} 1' in text
    assert "ml_ingestion_oldest_pending_age_seconds 5.000" in text
    assert "ml_ingestion_reconciliation_exhausted 0" in text
    assert owner.tenant_id not in text and owner.actor_id not in text
    assert job.job_id not in text and "source_id" not in text
    assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    finished = reader.render()
    assert 'ml_ingestion_jobs{state="PUBLISHED"} 1' in finished
    assert 'ml_ingestion_attempts{state="PUBLISHED"} 1' in finished
    assert "ml_ingestion_oldest_pending_age_seconds 0.000" in finished


def test_ingestion_metrics_flag_exhausted_reconciliation_and_hide_invalid_state(
    components, actor, tmp_path
):
    ingress, _, owner, _, _ = ingress_environment(components, actor, tmp_path)
    job = ingress.service.submit(owner, "exhausted-metrics", IngestionRequest("events", "v1"))
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs)
            .where(ingestion_jobs.c.id == job.job_id)
            .values(state="NEEDS_RECONCILIATION", error_code="RECONCILIATION_BUDGET_EXHAUSTED")
        )
    assert (
        "ml_ingestion_reconciliation_exhausted 1"
        in SQLIngestionMetricsReader(ingress.database).render()
    )
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs)
            .where(ingestion_jobs.c.id == job.job_id)
            .values(state='secret-source"\nunsafe-label')
        )
    text = SQLIngestionMetricsReader(ingress.database).render(now=time.time())
    assert 'ml_ingestion_jobs{state="UNKNOWN"} 1' in text
    assert "secret-source" not in text and "unsafe-label" not in text
