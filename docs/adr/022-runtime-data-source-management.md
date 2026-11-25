# ADR-022 Runtime-managed sources and deployment-time connections

Date 2026-11-25

Status implemented for review. External Databricks and production qualification remain separate.

## Context and decision

The legacy databricks-sql@1 adapter resolves a startup-loaded template reference.
SQLSourceCatalog already stores immutable tenant/source/version definitions,
configuration digests and mutable enablement. Reuse its definition_json and
configuration_digest columns. No schema migration is required.

Retain adapter version 1, Template validation, query construction and dbx-jsonl-v1.
Introduce the explicit configuration discriminator databricks-runtime-v1, containing
the complete typed Template, its public connection/template fingerprint and
serialization_version. The discriminator-free legacy template_ref/template_digest
envelope follows its original path. Unknown discriminators fail closed. Existing
records, digests and historical snapshot identities are preserved.

The application supplies the complete immutable SourceDefinition to the adapter.
Each worker resolves the exact SQL version through the existing SourceResolver.
The adapter performs no database access or mutable runtime source caching.

SourceManagementService uses narrow management-repository and approval ports.
Authenticated strict API DTOs delegate authorization and registration to it.
Infrastructure supplies typed Databricks approval and SQL persistence.

~~~mermaid
flowchart LR
  Ops[Deployment operator] --> Connections[Approved connections and credentials]
  Ops --> Policy[Tenant and table access policy]
  User[Source operator] --> API[Existing Management API]
  API --> Service[SourceManagementService]
  Service --> Policy
  Service --> Catalog[(Existing SQLSourceCatalog)]
  Client[Ingestion client] --> Jobs[Existing ingestion API and SQL jobs]
  Jobs --> Catalog
  Catalog --> Worker[Existing Ingestion Worker]
  Worker --> Policy
  Worker --> Adapter[DatabricksSQLBatchAdapter]
  Connections --> Adapter
  Adapter --> Warehouse[Fixed Delta version]
  Warehouse --> Raw[Existing immutable Raw Snapshot publication]
~~~

## Authorization and credential boundary

A deployment-owned databricks-access-v1 policy grants tenant-scoped catalog/schema/table-prefix access,
manager IDs, execution actor IDs, typed column subsets, required predicates and
maximum extraction budgets. Registration requires ml:source:manage plus a manager
grant. Execution requires ml:ingest, immutable source actor grants and a current
policy grant. Every source must name a nonempty subset of one grant's execution
actors. Unknown access is denied.

The grant Template's delta_version is structurally validated example metadata.
It does not restrict history selection. Each runtime source freezes its own
nonnegative Delta version. External retention and availability require live checks.

The bounded policy file is read on each authorization check. Deployment operators
own its filesystem permissions and propagation. Runtime APIs cannot edit grants
or connections. Management validates metadata without reading credential files,
connecting to a Warehouse or receiving worker-only secrets. A changed public
connection identity fails fingerprint verification for existing source versions.

Without a policy, runtime registration is denied. Legacy sources retain their
existing approved template and actor/enablement contracts. New management API
access to a legacy source additionally requires a matching policy grant.

## Lifecycle, compatibility and audit

Same-version equivalent registration returns the existing resource. Different
content returns 409. Replay never re-enables a disabled version. Every version
uses ingestion.source.register. Enable/disable retain their existing actions.

New source audit subjects use configuration_digest, which binds tenant, source
ID, version and full immutable content. Join subject_id to the tenant's source
catalog to recover exact identity. Existing actor, tenant, timestamp and action
columns remain in use. Historical audit rows are retained.

New submissions and acquisition attempts reject disabled sources and revoked
grants. Disablement does not promise immediate termination of an in-flight remote
query. Durable publication intents retain their existing reconciliation semantics,
finishing their original staged candidate without a new external extraction.
Published immutable snapshots are retained.

Initialization defaults to runtime mode and requests connection information only.
It writes a deny-by-default policy and an empty legacy template placeholder.
Existing templates default to keep, with explicit import/create/empty options.
All existing files use the existing private safe-write and backup flow.
Bootstrap also supports an omitted legacy template path. Compose keeps the
placeholder mount to preserve existing profile contracts.

## Alternatives and consequences

A new microservice or distributed template synchronization mechanism would add
deployment and consistency boundaries. Adapter-owned SQL lookups would duplicate
the existing application resolution. Both were rejected in favor of immutable
SQL catalog content and explicit injection.

Registration reports external_qualification=not_verified. Fixture integration
does not qualify real Warehouse permissions, OAuth, schema availability,
Databricks Kubernetes operation or production readiness. Review the
[implementation report](../reviews/runtime-source-management-report.md).
