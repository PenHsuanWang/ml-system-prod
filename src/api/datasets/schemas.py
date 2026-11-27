"""Dataset metadata and redacted-or-verified provenance response contracts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.api.collection_schemas import CollectionQuery

DatasetKey = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class DatasetFamilyQuery(CollectionQuery):
    dataset_id: DatasetKey | None = None


class DatasetFamily(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dataset_id: str
    version_count: int
    last_published_at: float


class BuilderIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    version: str


class SplitCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    training: int = Field(ge=0)
    validation: int = Field(ge=0)
    test: int = Field(ge=0)


class FeatureConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["fdc_spectrogram_v1"]
    log1p: bool


class DatasetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dataset_id: str
    version: str
    visibility: Literal["public", "tenant"]
    content_digest: str = Field(
        description="SHA-256 of immutable dataset content, not its provenance."
    )
    created_at: float = Field(description="UTC Unix publication time.")
    dataset_schema_version: str | None
    feature_schema_version: str | None
    feature_shape: list[int] | None = Field(
        description="Per-sample dimensions in stored axis order."
    )
    features: list[str] | None
    signal_order: list[str] | None
    pad_order: list[str] | None
    feature_config: FeatureConfiguration | None
    samples: SplitCounts | None
    lineage_status: Literal["recorded", "incomplete"] = Field(
        description="Recorded means a stored receipt; lineage detail verifies identity and access."
    )
    builder: BuilderIdentity | None


class ReceiptDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    version: str
    content_digest: str


class ReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["fdc", "wafer"]
    snapshot_id: str
    manifest_digest: str
    content_digest: str
    source_id: str
    source_version: str
    source_configuration_digest: str
    source_revision: str
    data_format: str
    schema_version: str
    size_bytes: int
    record_count: int


class ReceiptConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipe_id: str
    segment_id: int
    split_seed: int
    log1p: bool
    raw_input_schema: Literal["ebm-raw-input-v1"]
    alignment_policy: Literal["common_interval_utc_1hz_linear_no_extrapolation_v1"]
    split_policy: Literal["sha256_lot_approximately_60_20_20_v1"]
    feature_version: Literal["fdc_spectrogram_v1"]


class BuildReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt_version: Literal["ebm-dataset-build-receipt-v1"]
    builder: BuilderIdentity
    tenant_id: str
    dataset: ReceiptDataset
    inputs: list[ReceiptInput]
    configuration: ReceiptConfiguration


class DatasetLineage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dataset_id: str
    version: str
    content_digest: str
    status: Literal["complete", "incomplete", "restricted"]
    reason: (
        Literal["legacy_metadata_missing", "input_access_required", "receipt_integrity_failed"]
        | None
    )
    receipt_digest: str | None = Field(
        description="Canonical receipt SHA-256; withheld for incomplete or restricted evidence."
    )
    receipt: BuildReceipt | None
