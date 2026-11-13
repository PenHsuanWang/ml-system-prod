from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.datasets.ebm_builder import (
    EBMRawSnapshotReference,
    EBMRawSnapshotSet,
    EBMVerifiedRawSnapshot,
)
from src.domain.ingestion.models import (
    ExtractionLimits,
    IngestionRequest,
    RawSnapshot,
    SourceDefinition,
)
from src.domain.ml import canonical_json, digest
from src.infrastructure.persistence.ml_database import metadata


def raw_records(
    *, lot_count: int = 5, wafers_per_lot: int = 2
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    fdc: list[dict[str, object]] = []
    wafers: list[dict[str, object]] = []
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    for lot_index in range(lot_count):
        lot_id = f"lot-{lot_index:02d}"
        start = origin + timedelta(days=lot_index)
        for signal_index, signal in enumerate(EBM_SIGNAL_ORDER):
            for pad_index, pad in enumerate(EBM_PAD_ORDER):
                base = float(lot_index + 1) + signal_index / 10 + pad_index / 100
                fdc.append(
                    {
                        "schema_version": "ebm-raw-input-v1",
                        "record_type": "fdc_trace",
                        "lot_id": lot_id,
                        "recipe_id": "recipe-a",
                        "segment_id": 1,
                        "signal": signal,
                        "pad": pad,
                        "observations": [
                            {
                                "timestamp": (start + timedelta(seconds=offset)).isoformat(),
                                "value": base + offset / 4,
                            }
                            for offset in (0, 2, 4)
                        ],
                    }
                )
        for wafer_index in range(wafers_per_lot):
            wafers.append(
                {
                    "schema_version": "ebm-raw-input-v1",
                    "record_type": "wafer_target",
                    "sample_id": f"sample-{lot_index:02d}-{wafer_index:02d}",
                    "lot_id": lot_id,
                    "recipe_id": "recipe-a",
                    "segment_id": 1,
                    "wafer_id": f"wafer-{wafer_index:02d}",
                    "event_time": (start + timedelta(seconds=4)).isoformat(),
                    "available_at": (start + timedelta(seconds=5)).isoformat(),
                    "cdf_target": 0.1 + lot_index * 0.1 + wafer_index * 0.01,
                    "pad_counts": [
                        float(lot_index + wafer_index + pad_index + 1) for pad_index in range(4)
                    ],
                }
            )
    return fdc, wafers


def jsonl(records: list[dict[str, object]]) -> bytes:
    return ("".join(canonical_json(record) + "\n" for record in records)).encode("utf-8")


def _snapshot(role: str, records: list[dict[str, object]], tenant_id: str) -> RawSnapshot:
    payload = jsonl(records)
    content_digest = hashlib.sha256(payload).hexdigest()
    snapshot_id = "snapshot-" + role
    return RawSnapshot(
        snapshot_id,
        tenant_id,
        "source-" + role,
        "v1",
        digest(["source", role]),
        content_digest,
        digest(["manifest", role, content_digest]),
        "/approved/" + snapshot_id + "/manifest.json",
        "jsonl",
        "raw-json-object-v1",
        len(payload),
        len(records),
        1.0,
        "job-" + role,
        "sha256:" + content_digest,
    )


def verified_snapshot_set(
    fdc: list[dict[str, object]],
    wafers: list[dict[str, object]],
    *,
    tenant_id: str = "tenant-one",
    reverse: bool = False,
) -> EBMRawSnapshotSet:
    values = []
    for role, records in (("fdc", fdc), ("wafer", wafers)):
        snapshot = _snapshot(role, records, tenant_id)
        reference = EBMRawSnapshotReference(role, snapshot.snapshot_id, snapshot.manifest_digest)
        values.append(EBMVerifiedRawSnapshot(reference, snapshot, tuple(records)))
    if reverse:
        values.reverse()
    return EBMRawSnapshotSet(tuple(values))


@dataclass
class PublishedBuilderInputs:
    ingress: object
    operator: object
    fdc_snapshot: RawSnapshot
    wafer_snapshot: RawSnapshot
    fdc_path: Path
    wafer_path: Path
    fdc_records: list[dict[str, object]]
    wafer_records: list[dict[str, object]]

    @property
    def references(self) -> tuple[EBMRawSnapshotReference, ...]:
        return (
            EBMRawSnapshotReference(
                "fdc", self.fdc_snapshot.snapshot_id, self.fdc_snapshot.manifest_digest
            ),
            EBMRawSnapshotReference(
                "wafer", self.wafer_snapshot.snapshot_id, self.wafer_snapshot.manifest_digest
            ),
        )


@pytest.fixture
def published_builder_inputs(components, actor, tmp_path):
    fdc, wafers = raw_records()
    incoming = tmp_path / "builder-input"
    incoming.mkdir()
    fdc_path = incoming / "fdc.jsonl"
    wafer_path = incoming / "wafer.jsonl"
    fdc_path.write_bytes(jsonl(fdc))
    wafer_path.write_bytes(jsonl(wafers))
    settings = IngestionSettings(
        database_url=components.settings.database_url,
        raw_root=tmp_path / "raw-snapshots",
        file_roots=(("builder-input", incoming),),
        lease_seconds=3,
        retry_delay=0,
    )
    ingress = build_ingestion(settings, database=components.database)
    metadata.create_all(components.database.engine)
    operator = replace(
        actor,
        permissions=actor.permissions
        | {
            "ml:source:manage",
            "ml:ingest",
            "ml:ingestion:read",
            "ml:ingestion:operate",
            "ml:dataset:manage",
        },
    )
    snapshots = []
    for source_id, relative_path, key in (
        ("builder-fdc", "fdc.jsonl", "builder-fdc"),
        ("builder-wafer", "wafer.jsonl", "builder-wafer"),
    ):
        source = SourceDefinition(
            source_id,
            "v1",
            actor.tenant_id,
            "file",
            "1",
            json.dumps({"root_key": "builder-input", "relative_path": relative_path}),
            limits=ExtractionLimits(4 * 1024 * 1024, 1000, 64 * 1024, 5),
            allowed_actor_ids=(actor.actor_id,),
        )
        ingress.registry.validate_source(source)
        ingress.sources.register(operator, source)
        job = ingress.service.submit(operator, key, IngestionRequest(source_id, "v1"))
        assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
        snapshots.append(ingress.service.result(operator, job.job_id))
    try:
        yield PublishedBuilderInputs(
            ingress,
            operator,
            snapshots[0],
            snapshots[1],
            fdc_path,
            wafer_path,
            fdc,
            wafers,
        )
    finally:
        ingress.close()
