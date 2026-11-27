"""Digest-verified EBM build evidence bound to an immutable dataset identity."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from src.domain.datasets.ebm_builder import (
    EBM_DATASET_BUILDER_ID,
    EBM_DATASET_BUILDER_VERSION,
    EBMDatasetBuilderConfig,
)
from src.domain.ingestion.models import identifier, sha256
from src.domain.ml import SchemaMismatch, canonical_json

INPUT_KEYS = frozenset(
    {
        "role",
        "snapshot_id",
        "manifest_digest",
        "content_digest",
        "source_id",
        "source_version",
        "source_configuration_digest",
        "source_revision",
        "data_format",
        "schema_version",
        "size_bytes",
        "record_count",
    }
)


@dataclass(frozen=True)
class DatasetBuildReceipt:
    receipt_json: str
    receipt_digest: str

    def __post_init__(self) -> None:
        sha256(self.receipt_digest)
        if (
            len(self.receipt_json.encode()) > 65536
            or hashlib.sha256(self.receipt_json.encode()).hexdigest() != self.receipt_digest
        ):
            raise SchemaMismatch("Dataset build receipt digest mismatch")
        try:
            value = json.loads(self.receipt_json)
            if set(value) != {
                "receipt_version",
                "builder",
                "tenant_id",
                "dataset",
                "inputs",
                "configuration",
            }:
                raise ValueError
            if value["receipt_version"] != "ebm-dataset-build-receipt-v1":
                raise ValueError
            if value["builder"] != {
                "id": EBM_DATASET_BUILDER_ID,
                "version": EBM_DATASET_BUILDER_VERSION,
            }:
                raise ValueError
            identifier(value["tenant_id"])
            identity = value["dataset"]
            if set(identity) != {"id", "version", "content_digest"}:
                raise ValueError
            identifier(identity["id"])
            identifier(identity["version"])
            sha256(identity["content_digest"])
            inputs = value["inputs"]
            if not isinstance(inputs, list) or not 2 <= len(inputs) <= 32:
                raise ValueError
            seen = set()
            for item in inputs:
                if set(item) != INPUT_KEYS or item["role"] not in {"fdc", "wafer"}:
                    raise ValueError
                for key in (
                    "snapshot_id",
                    "source_id",
                    "source_version",
                    "data_format",
                    "schema_version",
                ):
                    identifier(item[key])
                for key in ("manifest_digest", "content_digest", "source_configuration_digest"):
                    sha256(item[key])
                if (
                    not isinstance(item["source_revision"], str)
                    or not 1 <= len(item["source_revision"]) <= 256
                ):
                    raise ValueError
                for key in ("size_bytes", "record_count"):
                    if type(item[key]) is not int or item[key] < 0:
                        raise ValueError
                if item["snapshot_id"] in seen:
                    raise ValueError
                seen.add(item["snapshot_id"])
            if {item["role"] for item in inputs} != {"fdc", "wafer"}:
                raise ValueError
            config = value["configuration"]
            expected_policies = {
                "raw_input_schema": "ebm-raw-input-v1",
                "alignment_policy": "common_interval_utc_1hz_linear_no_extrapolation_v1",
                "split_policy": "sha256_lot_approximately_60_20_20_v1",
                "feature_version": "fdc_spectrogram_v1",
            }
            if set(config) != set(expected_policies) | {
                "recipe_id",
                "segment_id",
                "split_seed",
                "log1p",
            }:
                raise ValueError
            if any(config[k] != v for k, v in expected_policies.items()):
                raise ValueError
            EBMDatasetBuilderConfig(
                **{k: config[k] for k in ("recipe_id", "segment_id", "split_seed", "log1p")}
            )
            if self.receipt_json != canonical_json(value):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise SchemaMismatch("Invalid dataset build receipt") from None

    def document(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.receipt_json))
