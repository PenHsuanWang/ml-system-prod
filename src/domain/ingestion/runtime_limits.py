"""One set of queue bounds shared by configuration and persistence."""

import math


def _duration(name: str, value: float, maximum: float, *, allow_zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value == 0
        and not allow_zero
        or value > maximum
    ):
        raise ValueError(f"Invalid ingestion {name}")


def validate_job_limits(
    *,
    lease_seconds: float,
    retry_delay: float,
    queue_timeout: float,
    max_attempts: int,
    reconcile_limit: int,
    max_pending: int,
) -> None:
    _duration("lease_seconds", lease_seconds, 3600)
    _duration("retry_delay", retry_delay, 300, allow_zero=True)
    _duration("queue_timeout", queue_timeout, 86400)
    for name, value, maximum in (
        ("max_attempts", max_attempts, 100),
        ("reconcile_limit", reconcile_limit, 1000),
        ("max_pending", max_pending, 100000),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"Invalid ingestion {name}")


def validate_poll_seconds(value: float) -> None:
    _duration("poll_seconds", value, 300)
