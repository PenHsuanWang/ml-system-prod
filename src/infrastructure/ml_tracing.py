"""W3C tracing without payloads, baggage, credentials or automatic exception text.

Propagation follows https://opentelemetry.io/docs/languages/python/propagation/.
The API is a no-op until bootstrap installs an explicit SDK/exporter.
"""

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.shared_models.context import current_request_id, job_id_context, request_id_context
from src.utils.validation import is_valid_request_id

propagator = TraceContextTextMapPropagator()


def capture_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagator.inject(carrier)
    request_id = current_request_id()
    if request_id:
        carrier["x-request-id"] = request_id
    return carrier


@contextmanager
def operation(name: str, *, carrier: Mapping[str, str] | None = None,
              attributes: dict[str, str | int] | None = None) -> Iterator[trace.Span]:
    context = propagator.extract(dict(carrier)) if carrier is not None else None
    with trace.get_tracer("ml-system-ebm").start_as_current_span(
        name, context=context, attributes=attributes, record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException:
            span.set_status(StatusCode.ERROR)
            raise


class MLJsonFormatter(logging.Formatter):
    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role

    def format(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        value = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                 "level": record.levelname, "logger": record.name,
                 "message": record.getMessage(), "role": self.role,
                 "request_id": current_request_id(), "job_id": job_id_context.get(),
                 "trace_id": format(context.trace_id, "032x") if context.is_valid else None,
                 "span_id": format(context.span_id, "016x") if context.is_valid else None}
        if record.exc_info and record.exc_info[0]:
            value["exception_type"] = record.exc_info[0].__name__
        return json.dumps(value)


class MLTraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        candidate = headers.get("x-request-id", "")
        request_id = candidate if is_valid_request_id(candidate) else uuid4().hex
        carrier = {name: headers[name] for name in ("traceparent", "tracestate")
                   if name in headers and len(headers[name]) <= 512}
        token = request_id_context.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        with trace.get_tracer("ml-system-ebm").start_as_current_span(
            "ml.http", context=propagator.extract(carrier), kind=SpanKind.SERVER,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            async def send_response(message: Message) -> None:
                if message["type"] == "http.response.start":
                    span.set_attribute("http.response.status_code", message["status"])
                    if message["status"] >= 500:
                        span.set_status(StatusCode.ERROR)
                    MutableHeaders(scope=message)["x-request-id"] = request_id
                await send(message)
            try:
                await self.app(scope, receive, send_response)
            except BaseException:
                span.set_status(StatusCode.ERROR)
                raise
            finally:
                route = scope.get("route")
                span.set_attribute("http.route", getattr(route, "path", "unmatched"))
                span.set_attribute("ml.request_id", request_id)
                request_id_context.reset(token)
