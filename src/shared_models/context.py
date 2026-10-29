"""Process-local correlation context; contains no identity tokens or task payloads."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from src.utils.validation import is_valid_request_id

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
job_id_context: ContextVar[str] = ContextVar("job_id", default="-")
_JOB_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def current_request_id() -> str | None:
    value = request_id_context.get()
    return value if value != "-" and is_valid_request_id(value) else None


@contextmanager
def bind_job_context(job_id: str, request_id: str | None) -> Iterator[None]:
    """Isolate each delivery, including legacy jobs with no request correlation."""
    request_token = request_id_context.set(
        request_id if request_id is not None and is_valid_request_id(request_id) else "-"
    )
    job_token = job_id_context.set(job_id if _JOB_ID.fullmatch(job_id) else "-")
    try:
        yield
    finally:
        job_id_context.reset(job_token)
        request_id_context.reset(request_token)
