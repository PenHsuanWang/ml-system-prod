# Governed discovery engineering report

## A. Repository baseline

Master e1dc4234c32c6586b31aed5a73d977abaa13348f, containing merged PR #14.
Work package A, branch feature/governed-databricks-discovery.
Implementation commit bd651e055f9e6ced25303a3bef1f30c81da3a5c6. Final branch head is the
subsequent report/evidence commit. No dependency on work packages B or C.

## B. Requirement traceability

| Requirement | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| US-A01 approved connections | CatalogService + independent policy | catalog authorization/API | UNIT_TESTED |
| US-A02 catalogs/schemas | Fixed INFORMATION_SCHEMA queries | real adapter fixture/API | INTEGRATION_TESTED |
| US-A03 tables/columns/types | Bounded SQL metadata + native unsupported types | adapter fixtures | INTEGRATION_TESTED |
| US-A04 existing registration/fixed Delta | Existing source routes unchanged, version_discovery unavailable | ingestion regression | UNIT_TESTED |
| US-A05 eligible sources | Shared approved_source admission, bounded tenant candidate port | SQL/API negative tests, PostgreSQL | INTEGRATION_TESTED |
| US-A06 permissions/tenant/columns | ml:catalog:read + databricks-discovery-v1 | authorization negatives and revoke-during-request | UNIT_TESTED |
| Cancellation/resources/errors | Spawned child, hard timeout, retained capacity, byte/row caps | physical cleanup/oversize tests | UNIT_TESTED |
| Credential isolation/OAuth/PAT | Separate bootstrap config and Management-only overlay | bootstrap and actual Compose rendering | INTEGRATION_TESTED |
| Existing contracts | No migration, unchanged legacy digest/state machines | 1360-test regression, initialization render | INTEGRATION_TESTED |
| New external qualification | No enterprise credential used | Not executed | EXTERNAL_ENVIRONMENT_NOT_VERIFIED |
| K8s discovery runtime | Architecture unchanged | No dynamic cluster execution | DEFERRED |

## C. Changed files

A = added, M = modified. No deleted files. Evidence/report files are additive
review artifacts; manifest records every tracked file.

| File | Type | Purpose | Architecture impact |
| --- | --- | --- | --- |
| FILE-MANIFEST.json | M | Documentation and reproducible inventory | Narrow additive capability |
| README.md | M | Documentation and reproducible inventory | Narrow additive capability |
| bootstrap/catalog_wiring.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| bootstrap/ingestion_wiring.py | M | Eligible-source admission/listing | Narrow additive capability |
| bootstrap/ml_api.py | M | Documentation and reproducible inventory | Narrow additive capability |
| compose.ml.catalog.yaml | A | Management-only optional Discovery Identity deployment | Narrow additive capability |
| docs/adr/023-governed-metadata-discovery.md | A | Documentation and reproducible inventory | Narrow additive capability |
| docs/adr/README.md | M | Documentation and reproducible inventory | Narrow additive capability |
| docs/architecture.md | M | Documentation and reproducible inventory | Narrow additive capability |
| docs/configuration-reference.md | M | Documentation and reproducible inventory | Narrow additive capability |
| docs/implementation-status.md | M | Documentation and reproducible inventory | Narrow additive capability |
| docs/plans/governed-discovery.md | A | Documentation and reproducible inventory | Narrow additive capability |
| docs/runtime-source-management.md | M | Documentation and reproducible inventory | Narrow additive capability |
| scripts/check_ingestion.py | M | Eligible-source admission/listing | Narrow additive capability |
| src/api/catalog/__init__.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/api/catalog/routers.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/api/catalog/schemas.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/api/ingestion/routers.py | M | Eligible-source admission/listing | Narrow additive capability |
| src/api/ingestion/schemas.py | M | Eligible-source admission/listing | Narrow additive capability |
| src/application/catalog/__init__.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/application/catalog/ports.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/application/catalog/service.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/application/ingestion/ports.py | M | Eligible-source admission/listing | Narrow additive capability |
| src/application/ingestion/service.py | M | Eligible-source admission/listing | Narrow additive capability |
| src/domain/catalog/__init__.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/domain/catalog/models.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/domain/ml.py | M | Documentation and reproducible inventory | Narrow additive capability |
| src/infrastructure/catalog/__init__.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/infrastructure/catalog/databricks_metadata.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/infrastructure/catalog/policy.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| src/infrastructure/ingestion/persistence/source_catalog.py | M | Eligible-source admission/listing | Narrow additive capability |
| tests/ml/catalog/conftest.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| tests/ml/catalog/test_available_sources.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| tests/ml/catalog/test_catalog_api.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| tests/ml/catalog/test_catalog_authorization.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| tests/ml/catalog/test_catalog_deployment.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |
| tests/ml/catalog/test_databricks_metadata.py | A | Metadata contracts, policy, bounded adapter and API | Narrow additive capability |

## D. Architecture impact

~~~mermaid
flowchart LR
  User --> Management
  Management --> Discovery[CatalogService]
  Discovery --> Policy[Separate discovery grants]
  Discovery --> Metadata[Bounded metadata process]
  Identity[Dedicated discovery identity] --> Metadata
  Metadata --> Databricks
  Management --> Sources[Existing source management / SQL Catalog]
  Management --> Admission[Shared ingestion admission]
  Admission --> Worker[Existing separate Ingestion Worker]
  Worker --> Raw[Immutable Raw Snapshot]
~~~

Domain models and narrow policy/provider ports preserve dependency direction.
Management alone gains opt-in discovery credentials. Training, outbox, fencing,
ACK, serving and source definition/digest contracts remain unchanged. Database
schema is unchanged. GET /sources keeps its management contract; new distinct
GET /available-sources uses ingestion permission. Discovery absence leaves existing
startup operational. There is no automatic source registration or execution.

## E. Security review

Tenant/actor/scope/column grants are denied by default and separate from execution.
Scopes are bound into fixed metadata SQL before the remote request and filtered
again after a policy recheck. No caller SQL, endpoint or credential is accepted.
OAuth/PAT reuse official verified connections, with separate reserved identity
references and no worker fallback. Native types are retained, unsupported types
are explicitly marked. Metadata has two physical slots, 100-item pages, 256-KiB
responses and a hard deadline plus bounded termination. Cancellation does not
release capacity before physical cleanup. Logs/SDK output and public errors omit
credential details. Operators still own actual least-privilege external identity
permissions and consistent deployment policy distribution.

## F. Validation results

See [machine-readable evidence](../evidence/governed-discovery-20261126/qualification.json)
for commands, environment, counts, hashes and logs. Focused tests 40/0/0;
PostgreSQL tests 40/0/0; full local ML regression 1360/0/5. The earlier focused
catalog run passed 37, and combined ingestion/init run passed 583/0/1 before
additional deployment/OAuth tests were added. These overlapping suites must not
be summed. Ruff/format/mypy passed (52 typed source files). Boundary check passed.
Six initialization Compose variants and two dedicated-identity overlay variants
rendered with the actual Compose CLI. Documentation and manifest checks passed.
Initial development lint/type findings were corrected and the quality gate rerun.
No test assertions or security gates were removed.

## G. Remaining risks and blockers

Live Databricks and new Kubernetes discovery are unverified. The official metadata
path requires Unity Catalog system.information_schema visibility. Reliable Delta
history discovery is deliberately unavailable. Separate opaque tokens cannot be
proved to represent distinct external principals without operator qualification.
Existing distributed policy propagation remains deployment-owned. Standard Docker
and full Compose regression are additionally exercised by the existing remote CI;
this local evidence does not claim those remote checks passed.
production_accepted remains false. External metadata visibility and table schema
qualification must be completed before production deployment.

## H. Merge recommendation

READY FOR REVIEW for the engineering change set. No merge or production approval
is implied. Proposed PR title: feat(catalog): add governed Databricks metadata and
available source discovery. This report supplies the proposed PR description.

Remote CI initially caught the reusable pytest fixture import missing an explicit
export for Pyflakes. Added __all__ and reran Pyflakes. Discovery Compose-rendering
tests now use the existing separate host qualification scope because the Backend
acceptance image has no Docker CLI; the contracts jobs execute both cases on the
Docker-enabled host. No assertion or CI test coverage is removed.
