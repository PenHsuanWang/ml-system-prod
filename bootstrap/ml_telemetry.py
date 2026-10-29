"""Optional explicit OTLP configuration; no exporter connection on module import."""

import logging
import os

from src.infrastructure.ml_tracing import MLJsonFormatter

_configured = False


def configure_telemetry(role: str) -> None:
    global _configured
    if _configured:
        return
    for name in ("src", "bootstrap"):
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler())
        for handler in logger.handlers:
            handler.setFormatter(MLJsonFormatter(role))
        logger.setLevel(logging.INFO)
        logger.propagate = False
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider = TracerProvider(resource=Resource.create({"service.name": "ml-"+role}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=5),
                                                       max_queue_size=1024))
        trace.set_tracer_provider(provider)
    _configured = True
