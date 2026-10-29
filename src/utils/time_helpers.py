"""Pure UTC formatting helpers without runtime state."""

from datetime import UTC, datetime


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
