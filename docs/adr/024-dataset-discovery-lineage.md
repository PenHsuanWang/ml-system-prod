# ADR-024 — Dataset discovery and immutable build provenance

Status: accepted for WP3 implementation; production qualification remains separate.

## Context

The existing Dataset Catalog resolves exact known versions. EBM DatasetBuilder verifies raw
snapshots and publishes a canonical filesystem receipt, but the catalog does not bind that
evidence to a dataset version. API users cannot enumerate raw snapshots or dataset versions
and cannot discover source/builder lineage without operator-provided resource IDs/files.

## Decision

Extend the existing Management API, DatasetService, ingestion service and SQL catalogs.
Keep discovery and management/execution ports separate. Filter tenant/owner visibility,
source/state/time, deterministic ordering and page bounds in SQL. Public fixture visibility
and tenant shadowing remain compatible. Existing prediction/training/execution paths stay intact.

Add `dataset_build_lineage` in migration 0008, keyed to the existing dataset SQL primary key.
The trusted publisher re-reads its bounded immutable receipt, verifies canonical SHA-256,
and supplies a typed domain value. The catalog verifies exact dataset identity and published
input metadata/visibility, then inserts new dataset and receipt in one SQL transaction.
The receipt includes tenant, dataset content, builder/version/configuration and exact source
and raw snapshot revisions/digests. Tenant locking serializes conflicting registration.
Content and provenance hashes remain distinct; established provenance cannot be overwritten.

Dataset metadata uses existing dataset read permissions. Complete receipts additionally require
existing ingestion read and input owner/operate rules. Restricted receipts withhold all source
IDs and receipt digest. Missing or corrupt history is explicitly incomplete. Existing rows are
never assigned new historical evidence by retry. No credentials or internal storage references
appear in public receipts. No new infrastructure credentials, services or arbitrary queries.

## Alternatives

- Reading arbitrary filesystem receipts from HTTP would lack SQL identity/atomic publication
  and depend on runtime mounts. It is not the authoritative discovery path.
- Embedding or rewriting legacy summaries would change existing metadata and invent history.
- A separate lineage registry/service would duplicate tenant identity and publication authority.
- A cursor service is unnecessary for these bounded metadata collections; documented deterministic
  offset pages reuse current conventions and may shift on concurrent publication.

## Consequences and limits

SQL is authoritative for recorded provenance. Immutable files may remain unreferenced after a
failed SQL transaction, as with existing artifact publication. An explicit schema downgrade
removes new provenance evidence, not dataset content. Missing files are not probed by metadata
GETs; complete lineage is not live external readiness or fresh byte-integrity qualification.
Migration 0008 follows the stacked WP2 0007 dependency, with no parallel Alembic head.
Dataset creation remains the operator CLI; WP6 orchestration and WP4/WP5 APIs are outside WP3.
See the [workflow and rollback guidance](../ml-runbook.md#dataset-discovery-and-lineage-wp3)
and [implementation plan](../plans/dataset-discovery-lineage.md).
