"""Ingestion terminal evidence is a raw snapshot, never a model binding."""

from src.domain.ml import Conflict

TERMINAL = frozenset({"PUBLISHED", "FAILED", "CANCELLED"})
TRANSITIONS = {
    "QUEUED": {"ACQUIRING", "CANCELLED", "FAILED"},
    "ACQUIRING": {"VALIDATING", "RETRY_WAIT", "CANCELLED", "FAILED"},
    "VALIDATING": {"PUBLISHING", "RETRY_WAIT", "CANCELLED", "FAILED"},
    "PUBLISHING": {"PUBLISHED", "NEEDS_RECONCILIATION"},
    "RETRY_WAIT": {"ACQUIRING", "CANCELLED", "FAILED"},
    "NEEDS_RECONCILIATION": {"PUBLISHING"},
}


def validate_transition(before: str, after: str) -> None:
    if after not in TRANSITIONS.get(before, set()):
        raise Conflict("Illegal ingestion state transition")
