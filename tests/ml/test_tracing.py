"""Trace context survives delayed delivery without persisting request payloads."""

import json

import httpx
import pytest

from bootstrap.ml_api import create_app
from src.infrastructure.ml_tracing import operation
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


@pytest.fixture
def spans(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    yield exporter
    provider.shutdown()


@pytest.mark.asyncio
async def test_api_outbox_worker_context_survives_replay(components, actor, intent, spans):
    class Identity:
        async def resolve(self, token):
            return actor

    app = create_app(services=components.services, identity=Identity())
    headers = {"Authorization": "Bearer test-only", "Idempotency-Key": "trace",
               "x-request-id": "req-delayed-delivery",
               "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
               "baggage": "sensitive=never-persist"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/training/jobs", json={"spec": json.loads(intent)}, headers=headers)
        assert first.status_code == 202
        assert first.headers['x-request-id'] == 'req-delayed-delivery'
        second = await client.post("/api/v1/training/jobs", json={"spec": json.loads(intent)}, headers=headers)
        assert second.json()['job_id'] == first.json()['job_id']
    item = SQLOutboxRepository(components.database).lease(1)[0]
    carrier = json.loads(item.command.context_json)
    assert set(carrier) == {'traceparent', 'x-request-id'}
    assert carrier['x-request-id'] == 'req-delayed-delivery'
    with operation('ml.training.execute', carrier=carrier):
        pass
    completed = spans.get_finished_spans()
    server = next(span for span in completed if span.name == 'ml.http')
    consumer = next(span for span in completed if span.name == 'ml.training.execute')
    assert consumer.context.trace_id == int('0123456789abcdef0123456789abcdef', 16)
    assert consumer.parent.span_id == server.context.span_id
    assert all('sensitive' not in str(span.attributes) for span in completed)


def test_span_failure_excludes_exception_text(spans):
    with pytest.raises(ValueError):
        with operation('controlled.failure'):
            raise ValueError('token=never-export')
    span = spans.get_finished_spans()[0]
    assert span.status.status_code.name == 'ERROR'
    assert span.status.description is None
    assert not span.events
