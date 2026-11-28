"""Immutable public inference contracts and reviewed, versioned usage descriptions."""

import json
import re
from dataclasses import dataclass
from typing import Any

from src.domain.ml import SchemaMismatch, canonical_json, digest

CONTRACT_VERSION = "prediction-contract-v1"
CONTRACT_KEYS = frozenset(
    {
        "contract_version",
        "input_schema_version",
        "output_schema_version",
        "input_schema",
        "output_schema",
        "request_example",
        "limits",
        "preprocessing",
        "output_semantics",
        "registry_backend",
        "input_units",
    }
)


def contract_document(value: str) -> dict[str, Any]:
    if len(value.encode()) > 256 * 1024:
        raise SchemaMismatch("Public model contract exceeds its size limit")
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or set(data) != CONTRACT_KEYS:
            raise ValueError
        if data["contract_version"] != CONTRACT_VERSION:
            raise ValueError
        if data["registry_backend"] not in {"local", "mlflow"}:
            raise ValueError
        for key in (
            "input_schema",
            "output_schema",
            "request_example",
            "limits",
            "preprocessing",
            "input_units",
            "output_semantics",
        ):
            if not isinstance(data[key], dict):
                raise ValueError
        for key in ("input_schema_version", "output_schema_version"):
            if not isinstance(data[key], str):
                raise ValueError
        if any(type(limit) is not int for limit in data["limits"].values()):
            raise ValueError
        if any(
            unit is not None and not isinstance(unit, str) for unit in data["input_units"].values()
        ):
            raise ValueError
        canonical_json(data)
        return data
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SchemaMismatch("Invalid public model contract") from None


@dataclass(frozen=True)
class PublicModelContract:
    binding_id: str
    manifest_digest: str
    document_json: str

    def __post_init__(self) -> None:
        document = contract_document(self.document_json)
        if self.document_json != canonical_json(document):
            raise SchemaMismatch("Public model contract must use canonical serialization")
        if not re.fullmatch(r"[a-f0-9]{64}", self.manifest_digest):
            raise SchemaMismatch("Invalid model manifest digest")

    @property
    def contract_digest(self) -> str:
        return digest(
            [
                CONTRACT_VERSION,
                self.binding_id,
                self.manifest_digest,
                json.loads(self.document_json),
            ]
        )


USAGE_FIELDS = frozenset(
    {
        "display_name",
        "purpose",
        "applicable_conditions",
        "usage_notes",
        "support_info",
    }
)


def reviewed_usage(value: dict[str, Any]) -> str:
    if not isinstance(value, dict) or set(value) != USAGE_FIELDS:
        raise SchemaMismatch("Provide all reviewed usage fields")
    for text in value.values():
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= 2048
            or any(ord(c) < 32 or c in "<>" for c in text)
        ):
            raise SchemaMismatch("Reviewed usage must contain bounded plain text")
    return canonical_json(value)
