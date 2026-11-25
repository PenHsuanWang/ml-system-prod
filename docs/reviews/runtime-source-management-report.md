# Runtime source management implementation and review report

## A. Executive summary

Runtime Databricks sources can be registered through authenticated Management APIs
and used by independently running workers from the durable SQL source catalog.
Deployment-owned connection and access policy constrain table scope, typed columns,
required predicates, actor grants and extraction budgets. New sources within an
approved catalog/schema scope need no deployment file changes or process restart.

## B. Baseline information

- Baseline branch master.
- Baseline commit 8c9023b00828000287e169ac760afe57102d7353, fetched from origin before changes.
- Feature branch feature/runtime-data-source-management.
- Final implementation code/test commit 1290be7890a1836bea975f30aefc6b3d6edacb87.
- Documentation and evidence commits follow the implementation commit.
- No applicable AGENTS.md files were present in the workspace checkout ancestry.
- Repository authority included module-spec, architecture, developer guidance,
  ADR-016 and the existing ingestion/init/deployment and CI contracts.

## C. Before and after architecture

Before, SQL source definitions referred to a startup-loaded template mapping.
Runtime registrations now carry a discriminator, complete typed Template and
approved non-secret connection fingerprint in the existing immutable definition.
Legacy envelopes resolve through their original mapping.

The existing Management API, SQL catalog, Adapter Registry, Ingestion Supervisor,
bounded acquisition, publication and downstream ML boundaries retain their roles.
New components are SourceManagementService, strict source DTOs/router and the
deployment-policy approval adapter. The Management API reads metadata and policy.
Worker-only external credentials remain within the ingestion boundary.

~~~mermaid
flowchart LR
  Ops[Deployment operator] --> Conn[Connections and credential references]
  Ops --> Policy[Approved tenant and table scopes]
  User[Authenticated source operator] --> API[Management API]
  API --> Service[SourceManagementService]
  Service --> Policy
  Service --> SQL[(Existing SQLSourceCatalog)]
  Request[Existing source ID/version job request] --> Jobs[Existing ingestion jobs]
  Jobs --> Worker[Independent Ingestion Worker]
  SQL --> Worker
  Policy --> Worker
  Conn --> Adapter[Existing Databricks adapter]
  Worker --> Adapter
  Adapter --> Raw[Existing immutable Raw Snapshot pipeline]
~~~

The schema already stores definition_json, configuration_digest, tenant/source/
version and enabled. Database migration is NOT APPLICABLE. Historical records
and migration 0006 are preserved.

## D. Source code change inventory

| File | Change type | Reason | Impact |
| --- | --- | --- | --- |
| .env.ml.ingestion.example | Modified | Read-only policy mounts and optional policy settings | Existing deployment profiles and worker-only credentials |
| .env.ml.production.example | Modified | Read-only policy mounts and optional policy settings | Existing deployment profiles and worker-only credentials |
| README.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| bootstrap/ingestion_wiring.py | Modified | Compose management service and exact-version authorization dependencies | Explicit capability construction |
| bootstrap/init_cli.py | Modified | Connection-only default and private policy output | Compose/external setup with existing safe-write behavior |
| bootstrap/init_config.py | Modified | Connection-only default and private policy output | Compose/external setup with existing safe-write behavior |
| bootstrap/ml_api.py | Modified | Compose management service and exact-version authorization dependencies | Explicit capability construction |
| compose.ml.databricks-token.yaml | Modified | Read-only policy mounts and optional policy settings | Existing deployment profiles and worker-only credentials |
| compose.ml.databricks.yaml | Modified | Read-only policy mounts and optional policy settings | Existing deployment profiles and worker-only credentials |
| compose.ml.external-mlflow.yaml | Modified | Read-only policy mounts and optional policy settings | Existing deployment profiles and worker-only credentials |
| docs/adr/022-runtime-data-source-management.md | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/adr/README.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/architecture.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/configuration-reference.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/data-ingress-implementation.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/databricks-ingestion-runbook.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/deployment/day1-deployment-runbook.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/deployment/interactive-setup.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/evidence/runtime-sources-20261125/checks.json | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/evidence/runtime-sources-20261125/compose-result.json | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/evidence/runtime-sources-20261125/ebm-e2e.json | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/evidence/runtime-sources-20261125/qualification.json | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/implementation-status.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/microservice-contracts.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/ml-runbook.md | Modified | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/reviews/runtime-source-management-plan.md | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| docs/runtime-source-management.md | New | Capability documentation and qualification scope | Operator/reviewer guidance |
| src/api/ingestion/source_routers.py | New | Strict authenticated source DTOs and endpoints | Ingestion management HTTP only |
| src/api/ingestion/source_schemas.py | New | Strict authenticated source DTOs and endpoints | Ingestion management HTTP only |
| src/application/ingestion/ports.py | Modified | Narrow management/approval ports and service | Source governance use cases |
| src/application/ingestion/source_management.py | New | Narrow management/approval ports and service | Source governance use cases |
| src/infrastructure/ingestion/adapters/databricks_contract.py | Modified | Explicit runtime configuration envelope and legacy resolution | Complete immutable extraction configuration |
| src/infrastructure/ingestion/adapters/source_approval.py | New | Tenant/manager/execution/table/column/predicate/budget policy | Runtime authorization and deny-by-default registration |
| src/infrastructure/ingestion/persistence/source_catalog.py | Modified | Bounded metadata queries, policy hook and digest-linked audits | Existing SQL catalog and transactions |
| tests/ml/ingestion/runtime_source_fixture.py | New | API, policy, concurrency, subprocess and profile regressions | Automated engineering evidence |
| tests/ml/ingestion/test_ingestion_deployment.py | Modified | API, policy, concurrency, subprocess and profile regressions | Automated engineering evidence |
| tests/ml/ingestion/test_runtime_source_processes.py | New | API, policy, concurrency, subprocess and profile regressions | Automated engineering evidence |
| tests/ml/ingestion/test_source_management.py | New | API, policy, concurrency, subprocess and profile regressions | Automated engineering evidence |
| tests/ml/init_setup/test_cli.py | Modified | API, policy, concurrency, subprocess and profile regressions | Automated engineering evidence |
| tests/ml/init_setup/test_config.py | Modified | API, policy, concurrency, subprocess and profile regressions | Automated engineering evidence |
| docs/reviews/runtime-source-management-report.md | New | Required implementation report | Reviewer evidence |
| FILE-MANIFEST.json | Modified | Regenerated tracked file hashes | Source/document integrity |

No files were deleted. Existing recursive CI scopes already cover the new modules, so CI validation scopes required no changes. Session-only CA BuildKit mounts and readable copied-source
permissions adapt Docker builds to the Cloud host. Those adaptations are outside
the checkout and are recorded as qualification limitations.

## E. API contract

The [operator walkthrough](../runtime-source-management.md) contains all endpoint
definitions, permission requirements, request/response and login examples.

All eight management endpoints are authenticated and require ml:source:manage plus
a deployment-owned grant. Existing dot aliases remain compatible. Tenant comes from
Actor. POST /sources and POST /sources/{source_id}/versions return 201 on initial
registration and equivalent replay. Different same-version content returns 409.
Replay preserves disabled state. Configuration changes require a new version.

Lists use bounded limit/offset and lexicographic source/version ordering. Policy
filtering follows the bounded SQL page, with next_offset advancing the scanned
page. Responses expose typed table configuration and digest while keeping
connection endpoints and credentials private. Missing authentication, permission,
resource, immutable conflict and invalid HTTP DTOs use 401/403/404/409/422.
Domain contract errors use 400 and unconfigured management uses 503.
Registration always reports external_qualification=not_verified.

## F. Backward compatibility

Legacy template_ref/template_digest/serialization_version envelopes keep adapter
version 1 and their original fingerprint algorithm. Existing records and snapshot
lineage are retained. SourceDefinition outer digests continue binding all immutable
fields and extraction limits. Existing ingestion jobs still accept only source ID,
version and narrowing request limits.

OAuth and token clients retain their authentication paths. Management receives
public connection metadata and read-only policy. Secrets/tokens remain worker-only.
Compose retains the legacy placeholder mount. Bootstrap accepts an omitted legacy
template file. Initialization defaults to connection-only runtime mode and keeps
existing source templates by default. Private files retain confirmation, backups
and permissions. File Batch, downstream ML and Kubernetes File engineering
profiles continue using their existing contracts.

## G. Security review

- Tenant isolation uses Actor-derived tenant IDs and scoped SQL queries.
- Connection authorization uses deployment-owned references and manager grants.
- Optional catalog/schema/table-prefix scope and typed column subsets constrain
  new tables. Required predicates are preserved, with additional AND restrictions.
- Runtime APIs cannot redefine endpoints, credentials, policy or arbitrary SQL.
  Strict DTOs reject unknown fields. Existing parameterized query construction,
  identifier/type/operator validation and fixed Delta reads remain in use.
- Immutable configuration fingerprints bind the public connection identity.
  Host/Warehouse/client identity drift fails validation. Credential rotation paths
  and secret values remain outside fingerprints.
- API and operator CLI runtime registration/lifecycle operations both enforce deployment policy. Registration uses existing transactional tenant locks. Same-version conflicts
  and concurrent registration retain one immutable resource.
- Admission and acquisition recheck actor grants, current policy, enablement and
  source digest. Persisted content tampering fails closed.
- Audits retain tenant, actor, action and timestamp. New subject_id values are
  immutable configuration digests, joining to exact tenant/source/version identity.
- Policy changes are deployment operations. Read-only single-file mounts require
  deliberate propagation when host files are atomically replaced.
- Disablement and revocation reject new acquisition attempts. Immediate termination
  of an already-started remote query is not promised. Durable publication intents
  reconcile the same staged candidate under the existing recovery contract.

## H. Test evidence

The final command matrix below uses implementation commit
1290be7890a1836bea975f30aefc6b3d6edacb87. Structural gates have N/A test counts.
Full local logs and JUnit files are under var/runtime-source-evidence.
The checked-in evidence JSON preserves command, source revision, environment,
status and counts.

| Check | Exact command | Environment | Passed | Failed | Skipped | Exit | Local evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ingestion-quality | .venv/bin/python scripts/check_ingestion.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | ingestion-quality.log |
| init-quality | .venv/bin/python scripts/check_init.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | init-quality.log |
| boundaries | .venv/bin/python scripts/check_boundaries.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | boundaries.log |
| deployment-quality | .venv/bin/python scripts/check_deployment.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | deployment-quality.log |
| training-quality | .venv/bin/python scripts/check_training_runtime.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | training-quality.log |
| serving-quality | .venv/bin/python scripts/check_serving_runtime.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | serving-quality.log |
| ebm-quality | .venv/bin/python scripts/check_ebm_typing.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | ebm-quality.log |
| k8s-acceptance-contracts | .venv/bin/python scripts/check_k8s_acceptance.py | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | k8s-acceptance-contracts.log |
| compose-init-render | .venv/bin/python -m scripts.verify_init_compose | Cloud Python 3.12 / isolated SQLite | N/A | 0 | 0 | 0 | compose-init-render.log |
| regressions | .venv/bin/python -m pytest tests/ml tests/test_boundaries.py -q -rs --junitxml=var/runtime-source-evidence/regressions.xml | Cloud Python 3.12 / isolated SQLite | 1291 | 0 | 5 | 0 | regressions.log |
| postgres-runtime | .venv/bin/python -m pytest tests/ml/ingestion/test_source_management.py tests/ml/ingestion/test_runtime_source_processes.py -q --junitxml=var/runtime-source-evidence/postgres-final.xml | Cloud API and two worker processes / disposable PostgreSQL 17 Docker container | 21 | 0 | 0 | 0 | postgres-runtime.log |

Additional executed checks and integration evidence are recorded below.

- PASS, PATH=/workspace/runtime-source-tools:$PATH .venv/bin/python scripts/check_k8s.py,
  Cloud host with checksum-verified kubectl v1.37.2, 40 rendered resources at implementation revision 1290be7890a1836bea975f30aefc6b3d6edacb87,
  test counts N/A, exit 0, k8s-foundation.log.
- BLOCKED, MINIKUBE_HOME=/workspace/runtime-source-kube
  KUBECONFIG=/workspace/runtime-source-kube/config
  /workspace/runtime-source-tools/minikube -p minikube start --driver=docker
  --container-runtime=containerd --kubernetes-version=v1.37.2 --cpus=2 --memory=6144.
  Cloud host, checksum-verified Minikube v1.39.0, exit 40,
  k8s-start.log. Version lookup at api.github.com returned Forbidden.
- NOT VERIFIED, real external Databricks OAuth/token/table qualification.
  Automated runtime integration replaces only the external SQL byte-stream client
  and exercises real HTTP, SQL catalog, independent worker processes and
  existing publication. No production credentials or enterprise data were used.
- Five local regression skips retain the existing runner's conditions for live
  Databricks, full backend/EBM services and RabbitMQ/Redis receiver services.
  They are not counted as local passes.

Development iterations also executed .venv/bin/python -m pytest tests/ml/ingestion
tests/ml/init_setup -q, with 495 passed and one live qualification skip before new
tests were added. Focused test_source_management.py initially passed 17 tests.
The two-file runtime source suite then passed 18 tests on SQLite and PostgreSQL.
Expanded security cases produce the final 21 PostgreSQL passes.

During development, check_ingestion.py caught an optional Template typing assignment,
Ruff caught test import/format issues, init tests caught two missing policy env
exports, and the full suite caught two deployment tests expecting the old mount
inventory. These were corrected at source. Final gates preserve all security
assertions and require the explicit third read-only policy mount.

PASS, bash /tmp/runtime-source-compose/run.sh run --rm --no-deps acceptance
python scripts/verify_ml_acceptance.py --require-stack --output-dir /app/var/acceptance.
The ml-runtime-source-final disposable project used the implementation code/test
revision 1290be7890a1836bea975f30aefc6b3d6edacb87. Python 3.12.16 and Torch 2.6.0+cpu
executed 1,284 cases with zero failures, errors or skips, exit 0.
The main suite passed 1,283 cases and deliberately deselected the single EBM case
for the runner's separate ordered EBM invocation, which passed one case.
Result FUNCTIONAL_BACKEND_ACCEPTED, production_accepted=false.
Full logs and JUnit reports are under var/runtime-source-compose-final/acceptance.
Checked-in compose-result.json, ebm-e2e.json and qualification.json include scope,
source revision and artifact checksums.

Session-only Docker adaptation supplied the Cloud CA through BuildKit secrets,
made copied source readable to non-root users and overlaid final source onto
valid installed images. Compose DNS names were appended to inherited NO_PROXY.
An initial stack start failed during bucket creation because internal HTTP used
the Cloud proxy, then succeeded after the internal proxy exclusion.
Two overlapping acceptance attempts were stopped with exit 143 and are
NOT VERIFIED. Their shared output was excluded. The accepted run used a new
project, new database volumes and a separate report directory with one runner.

PASS, .venv/bin/python scripts/check_docs.py, final staged documentation tree,
111 tracked documentation artifacts with valid links/fragments/SVG/JSON, exit 0.
PASS, .venv/bin/python scripts/update_manifest.py --check, final staged tree,
test counts N/A, exit 0. The manifest contains 569 tracked entries and identifies those exact file bytes.

No unexecuted gate is classified as passed.

## I. Acceptance criteria traceability

| Acceptance ID | Status | Implementation reference | Test evidence |
| --- | --- | --- | --- |
| AC-01 | PASS | Connection-only default and policy env retention | init profile tests and Compose render |
| AC-02 | PASS | Optional legacy template path and empty startup mapping | PostgreSQL subprocess fixture starts with no templates |
| AC-03 | PASS | SourceManagementService and strict source router | API/security tests |
| AC-04 | PASS | Existing SQLSourceCatalog definition_json | 21 tests on disposable PostgreSQL 17 |
| AC-05 | PASS | Canonical SourceDefinition and transactional tenant lock | Idempotency, conflict, concurrency and tamper tests |
| AC-06 | PASS | Execution-time exact SQL catalog lookup | Independent workers start before HTTP registration |
| AC-07 | PASS | Existing stager/publication pipeline | Snapshot payload, manifest, digest and source revision verification |
| AC-08 | PASS | Runtime immutable config and schema policy scope | Two different tables registered without file changes/restarts |
| AC-09 | PASS | Per-instance SQLSourceCatalog | Two workers and restarted API/worker processes |
| AC-10 | PASS | Legacy branch and unchanged snapshot contracts | Legacy contract, publication, DatasetBuilder and ML regressions |
| AC-11 | PASS | Eight source management endpoints | Registration, listing, version, enable and disable tests |
| AC-12 | PASS | Tenant-scoped repository and deployment-owned grants | Cross-tenant, ungranted manager, unknown connection and scope rejection |
| AC-13 | PASS | Existing Template/query builder and limits | SQL security, predicates, identifier and budget regressions |
| AC-14 | PASS | Metadata-only management and worker-only credentials | OAuth/token/external mount contract tests |
| AC-15 | PASS | Unmodified legacy envelope resolution | Existing static template tests |
| AC-16 | PASS | Existing OAuth/token authentication implementations | Init profiles and connector tests, live external authentication NOT VERIFIED |
| AC-17 | PASS | Existing FileBatchAdapter | File, HTTP and publication regressions |
| AC-18 | PASS | Existing jobs API and supervisor lifecycle | Ingestion, idempotency, recovery/fencing and downstream suites |
| AC-19 | PASS | Existing transactional audit ledger | Registration and lifecycle digest audit assertions |
| AC-20 | NOT VERIFIED for Kubernetes dynamic scope, deferred by user | Compose runtime and Kubernetes engineering gates | Compose FUNCTIONAL_BACKEND_ACCEPTED with 1,284 passed and zero skips. Kubernetes static contracts passed. User deferred Kubernetes dynamic acceptance for this code review on 2026-11-25. |
| AC-21 | PASS | ADR-022, walkthrough and scoped documentation | Documentation links and tracked manifest validation |
| AC-22 | PASS | Changes confined to source governance, setup, tests and docs | Baseline diff inventory and dependency boundary check |
| AC-23 | PASS | external_qualification=not_verified | API response assertions |
| AC-24 | PASS | Existing acceptance/production flags retained | Source diff and deployment regressions |

## J. Remaining risks and deferred scope

External Warehouse schema, retained Delta history and actual service-principal
privileges need live operator qualification. Registration approval alone does not
establish those facts. The integration fixture validates the platform boundary
with synthetic rows.

Policies require operator-controlled filesystem permissions and consistent
deployment propagation. Prefix grants should use the narrowest approved scope.
Source operators can select fixed history within that scope, while mandatory
predicates, column grants and budgets continue to constrain each definition.
List pagination is bounded and can shift under concurrent registration.

The user deferred Kubernetes dynamic engineering acceptance for this code review
on 2026-11-25. Its execution remains NOT VERIFIED. The previous version-lookup
network denial is retained as historical evidence. api.github.com is saved in
the environment draft, while the running network policy still excludes it.

No new adapters, discovery frontend, general SQL execution, orchestration service,
multi-stage approval workflow or ML runtime redesign is included. Those remain
outside this change.

## K. Release recommendation

READY FOR REVIEW under the user's 2026-11-25 instruction to defer Kubernetes
dynamic acceptance and open a Draft PR for code review.

Compose functional backend acceptance passed with 1,284 cases, zero failures
and zero skips. PostgreSQL runtime/security validation passed 21 cases.
Kubernetes static manifest contracts passed for 40 resources. Dynamic Kubernetes
acceptance remains NOT VERIFIED and is deferred for this review.
Production acceptance remains false. Live Databricks and production release
require their separate qualification and approval.

The feature branch is published as feature/runtime-data-source-management.
The requested Draft PR title is
feat(ingestion): introduce governed runtime data source management API.

GitHub API requests currently return 403 at api.github.com, including the
GraphQL operation required by gh pr create. The prepared PR body is retained
in var/runtime-source-evidence/pr-body.md. After pushing this review-scope
update, gh pr create --draft was retried and exited 1 with
POST https://api.github.com/graphql Forbidden. Draft PR creation remains blocked.

The api.github.com domain grant is already saved in the environment draft.
The available configuration tool saves drafts without applying the running
network policy. That runtime setting is the prerequisite for retrying the
GitHub API operation.
