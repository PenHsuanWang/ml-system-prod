# Governed discovery implementation plan

Baseline master e1dc4234c32c6586b31aed5a73d977abaa13348f.
Branch feature/governed-databricks-discovery. Investigation date 2026-11-26.

## Inventory and gaps

The existing SQLSourceCatalog resolves immutable tenant-scoped versions, checks
stored digests, enablement and actor grants, and invokes current runtime access
policy. IngestionService then validates the registered adapter and connection
fingerprint. Management source listing requires ml:source:manage and stays intact.
There is no ingestion-client listing or external metadata discovery. Databricks
SQL Connector 4.2.6 supports DB-API parameterized execute/fetchmany. Existing
spawned acquisition provides hard process termination and private OAuth/PAT
loading. Reuse authentication and cleanup helpers without changing acquisition.

## Design and APIs

Add available-sources using a narrow tenant-scoped candidate scan port and the
same application admission helper as submission. Scan at most limit+1 keys;
return next_offset/has_more even if a scanned page filters to zero items.
No external query or credential read is involved.

Add a catalog module with application policy/provider ports, pure metadata
contracts, strict HTTP DTOs and bootstrap wiring. Expose GET connections and
catalogs/schemas/tables/columns below /api/v1/catalog. Use fixed, parameterized
system.information_schema queries, bounded to approved scope before executing.
The response contains names/types/nullability/support only. Delta history is
explicitly unavailable; registration continues requiring a supplied fixed version.

A separate databricks-discovery-v1 policy grants tenant, actor, connection,
table-scope prefixes and columns. ml:catalog:read (alias ml.catalog.read) is
independent. No changes to databricks-access-v1 or stored sources/digests.
Opt-in discovery uses a separate connection configuration and dedicated OAuth
secret file or reserved discovery token environment reference. No fallback to
worker connections or credentials. Disabled discovery leaves Management usable.

Use synchronous routes in the HTTP threadpool and a bounded spawned SQL process.
A hard deadline, row/byte limits, fail-fast capacity, single connector attempt,
bounded pipe reads and physical reaping bound operations, including cancellation.
Reload policy and require the same effective grants after the external operation.
No metadata caching or new service/database. No external qualification claim.

## File scope and preserved contracts

New src/{domain,application,api,infrastructure}/catalog modules,
bootstrap/catalog_wiring.py, catalog tests, optional Compose discovery overlay,
runbook and ADR. Modify ingestion service/ports/catalog/DTO/router and wiring
only for available-source admission. Add permission alias and Management wiring.
Extend existing ingestion quality gate to catalog. Update manifest and docs.
No migration, source mutation, training, outbox, leases, ACK, registry, serving,
Kubernetes topology or existing initialization behavior changes.

## Validation and compatibility matrix

| Requirement | Evidence planned |
| --- | --- |
| US-A01/A02/A03 | Policy/provider/API tests and real SQL adapter fixture |
| US-A04 | Existing registration regression; explicit version-unavailable response |
| US-A05 | SQL-backed available listing vs submission eligibility; empty-page continuation |
| US-A06 | Permission/tenant/scope/column tests; dedicated credential wiring |
| Bounded execution | Timeout, oversized response, capacity and physical cleanup tests |
| Compatibility | Existing ingestion/init tests, boundaries, Ruff, mypy, documentation checks |
| Deployment | Real Compose rendering for OAuth/PAT overlays; worker credentials stay isolated |
| External | Live Warehouse unavailable unless explicitly approved; NOT VERIFIED |

The uploaded request also defines independent hybrid MLflow and optional corporate
build trust work packages. Implement each on its own branch from the same latest
master, after inspecting its concrete deployment/build contracts. They must not
depend on this catalog branch. Each receives a separate plan, evidence and report.
