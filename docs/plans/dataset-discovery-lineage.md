# WP3 — raw snapshot and dataset discovery / lineage

Baseline master 651ce17, WP2 dependency 75802fd. The initial WP3 branch merges that master
into the WP2 branch at dff1c07 so the new migration follows 0007 without a parallel Alembic head.
Branch feature/dataset-discovery-lineage. User authorization covers WP3 with sequential commits.
PR #19 later merged master at `eb472ca`, which includes the final WP2 review corrections.

## Scope and implementation

- Add bounded GET /api/v1/ingestion/jobs and /snapshots, with exact source/version,
  state (jobs), creation-time and page filters. Filter tenant and owner/operate visibility in SQL.
  Existing submission, detail, cancellation and immutable raw snapshot contracts remain in use.
- Add GET /api/v1/datasets, /{id}/versions, /{id}/versions/{version}, and
  /{id}/versions/{version}/lineage. Keep /versions/{dataset_id}@{version} compatible.
  Use the existing DatasetService and SQLDatasetCatalog. Preserve public built-in fixture visibility
  and private-over-public version resolution.
- Store the verified EBM builder receipt in an additive SQL table keyed to DatasetVersion.
  New dataset + provenance registration is one transaction. Content identity and receipt identity
  remain distinct. Same version/content with different established provenance conflicts.
- Existing rows without a SQL receipt remain explicitly incomplete. A retry of a legacy version
  does not assign historical provenance. New builds/new versions obtain verified receipt metadata.
- Dataset reads use ml:train or ml:dataset:read. Detailed raw-input lineage additionally respects
  the existing ingestion read permissions and owner/operate visibility. If input metadata is
  unauthorized, return restricted status with withheld receipt rather than exposing resource IDs.
- Return safe schemas, shape/configuration, split counts, publication time and digests.
  No raw files, artifact locations, internal paths or new data download endpoint.

## Design decisions

Use offset pages, default 25/max 100, maximum offset 1,000,000. Ordering is explicit and
deterministic; clients should tolerate changes between pages. Timestamp bounds use UTC Unix
seconds, strictly after/before. A page has items, limit, offset and nullable next_offset.
Continuation ends when the next offset would exceed the maximum, even if SQL found more rows.
SQL performs filtering, shadow resolution, grouping and bounds before records reach Python.

Migration 0008 follows WP2 migration 0007, now part of master. No published migration is rewritten.
The receipt is canonically serialized and SHA-256 verified against the immutable on-disk
receipt before the existing publisher supplies it to catalog registration. Catalog registration
verifies exact dataset identity/content, current SQL-published input metadata and actor visibility.
Receipt and dataset inserts roll back together; failed builds may leave unreferenced content
files as in the existing publisher. A filesystem receipt alone does not establish SQL lineage.
Stored receipts are immutable once bound. Legacy incomplete metadata is not silently adopted.

## Acceptance and verification

Exercise owner/read/operate authorization, cross-tenant IDs, public fixtures, private shadows,
empty/final pages, filters and unknown fields. Verify same-content/conflicting-receipt races,
corrupt receipt rejection, SQL atomicity, legacy incomplete and restricted lineage.
Run actual File ingestion → existing EBM builder → SQL catalog → HTTP discovery/detail/lineage
with a separate authenticated client. Preserve existing training and builder CLI regression.
Qualify migration upgrade/downgrade on disposable SQLite and PostgreSQL only.
Run applicable lint/type/boundary gates and full local ML regression; external Compose/K8s
and enterprise production qualification remain separate and production_accepted=false.

WP4–WP7 are not part of this branch. Dataset build orchestration remains the existing explicit CLI.
