# ADR-023 Governed metadata discovery with a dedicated identity

Date 2026-11-26. Status implemented for engineering review.

The immutable SQL Source Catalog describes approved extractions. It cannot expose
external catalogs that have not been registered. Introduce Catalog Discovery in
the existing Management runtime, with narrow application policy/provider ports.
No new service, cache, synchronization process or database schema is needed.

Use official SQL Connector 4.2.6 parameterized, fixed system.information_schema
queries. UC REST and unbounded connector metadata result sets were considered;
a single SQL path reuses existing OAuth/PAT authentication and physical process
cleanup while enforcing explicit SQL pagination. Reliable Delta history is not
part of this metadata path and is explicitly unavailable.

The databricks-discovery-v1 policy is independent of databricks-access-v1.
ml:catalog:read and exact tenant/actor/connection/scope/column grants are required.
Scopes constrain queries and responses, with a post-operation revocation check.
A separate opt-in least-privilege Discovery Identity is held only by Management;
worker ingestion credentials never become a fallback. Disabled discovery preserves
Management startup and existing source/ingestion APIs.

Blocking SDK calls run in a bounded spawned process, outside the event loop.
Capacity is retained until physical termination and pipe cleanup. Page, bytes,
time and concurrency limits apply. No SQL, external endpoint or credential enters
through HTTP. Metadata does not constitute source approval or live qualification.

Available-source listing uses bounded SQL candidate scans and the same admission
helper as ingestion submission, preserving resolve/for_execution and legacy actor
semantics. An empty filtered page can still have a continuation. All source
configuration digests, legacy contracts and downstream lifecycle remain unchanged.

See [operator workflow](../runtime-source-management.md#governed-runtime-metadata-discovery)
and [implementation plan](../plans/governed-discovery.md). External Databricks and
site-specific Kubernetes discovery remain separate qualification requirements.
