# Runtime source management implementation plan

Baseline branch `master`, commit `8c9023b00828000287e169ac760afe57102d7353`.

1. Extend the Databricks configuration envelope with an explicit runtime discriminator. Reuse Template validation, fingerprint, codec and bounded query generation. Preserve the legacy envelope byte identity.
2. Reuse SQLSourceCatalog storage and transactional tenant locks. Add bounded metadata queries and digest-bearing audit subjects. Add management and approval ports.
3. Add a management service and strict authenticated API DTOs. Apply deployment-owned tenant, manager, execution actor, exact table, typed column, required predicate and extraction budget grants. Check grants again on admission and execution.
4. Wire metadata-only approval dependencies without external SDK calls or credential reads. Allow omitted legacy template file. Make new initialization connection-only while retaining explicit legacy choices and existing files.
5. Test configuration, APIs, concurrency, revocation, independent SQL-backed worker instances and initialization. Run CI quality gates and isolated PostgreSQL/Compose checks. Record unavailable external/Kubernetes gates explicitly.
6. Update ADR, operator walkthrough, architecture references, manifest and final report. Commit in reviewable stages and create a draft PR if GitHub permits it.

## Acceptance traceability plan

| Criteria | Implementation and planned evidence |
| --- | --- |
| AC-01, AC-02 | Connection-only init and bootstrap tests for Compose/external, OAuth/token |
| AC-03, AC-11 | Source API service, strict DTOs, authentication, pagination and lifecycle tests |
| AC-04, AC-05 | Existing SQL catalog, concurrent immutable registration, PostgreSQL tests |
| AC-06, AC-07, AC-08, AC-09 | Separate running API/worker fixture integration, two registrations, verified snapshots and restarts |
| AC-10, AC-15, AC-17, AC-18 | Existing legacy, file, recovery/fencing, publication and downstream suites |
| AC-12, AC-13, AC-14 | Policy denial, injection, credentials and role mount regressions |
| AC-16 | OAuth/token initialization and adapter regression tests |
| AC-19 | Transactional audit assertions |
| AC-20 | Compose and Kubernetes gates with environment evidence |
| AC-21 | ADR, runbook, documentation and manifest checks |
| AC-22 | Scope review against baseline |
| AC-23, AC-24 | Explicit unqualified external status and unchanged production flags |
