# Day-1 deployment implementation and verification — 2026-11-10

This delivery addresses the Day-1 review of PR #1 at `5ec1c6f`. It preserves the
existing ports, application services and model lifecycle. The deployment target
is a single Linux host with approved external PostgreSQL, MongoDB, RabbitMQ,
Redis, MLflow and Databricks services.

## Review requirements delivered

| Requirement | Implementation |
| --- | --- |
| Unified installation entry | Root README distinguishes algorithm development, the reference stack and the external-service release profile. |
| External MLflow | Standalone `compose.ml.external-mlflow.yaml`; no bundled infrastructure or development overlay. HTTPS, approved CA and matching SDK/application endpoints are explicit. |
| Production release identity | Unified `production` image target with a complete source revision label. Offline preflight compares the rendered model against the actual image's RepoDigests, revision, platform, role settings and limits. |
| Role credentials | Private, owned 0400/0600 JSON files, strict role fields, a non-root UID, a private runtime home and read-only approved input mounts outside writable runtime storage. |
| Serving credentials | A separate reader Basic identity or token and CA enter the bounded Serving environment; writer/database/Databricks credentials do not. |
| Complete role startup | Explicit migration receipt, then Management, Relay, Supervisor, Controller, Gateway and Ingestion Worker. Operator tasks use a separate one-shot profile and secret-loading entrypoint. |
| Actual dependency qualification | `qualify-mlflow` retains a Run, Registry version and artifact, checks upload/download SHA256, and requires explicit denial of reader Tracking/Registry writes from a fresh process. |
| Operator SourceDefinition | Read-only definition directory, template digest, actual actor identity, registration receipt, controlled ingestion and independently expected Raw Snapshot digest. |
| Day-1 decision | Ordered runbook covers backups, migration, source registration, all roles, an approved DatasetVersion's model lifecycle, metrics, ten evidence requirements and rollback. |

`preflight` is offline configuration validation. Readiness and worker health do
not qualify MLflow permissions or Databricks OAuth/query access. All reports
retain `production_accepted=false`; the final Go decision belongs to the
approved deployment reviewers.

## Verification

The final local Python 3.12 / Torch 2.6 CPU regression executed **1,027 passing cases**
with zero failures. Four optional service cases were skipped because live
Databricks, Backend HTTP endpoints and RabbitMQ/Redis test URLs were not enabled
in that invocation. Those cases are not counted as accepted. The existing CI
Backend gate still requires its selected real-service cases and zero skips.

The **107 passing deployment-focused cases** cover private-file ownership and permissions, role
fields, supported credentials, TLS configuration, real Compose rendering,
Registry/source provenance and fail-closed qualification. Ruff, formatting,
strict deployment Mypy, Ingestion checks, EBM/core typing, Pyflakes and dependency
boundaries are separate gates. CI additionally builds and checks the actual
unified image, including its non-root secret-loading entrypoint; it does not
publish or approve a production Registry release.

The local unified image built from implementation commit
`0bd89b6d577dec7f726cfbe133f44e58de09a9d5` passed `verify_deployment_image.py`:
Python 3.12.16, Torch 2.6.0 CPU, SciPy 1.18.1 and Databricks SQL connector 4.2.6,
with `PRIVATE_CONFIGURATION_ENTRYPOINT_PASSED` under the host's non-root UID.
That image preceded the final database-URL guard and documentation commits;
the current-head CI rebuild is the check for the final source. The local
receipt is `/tmp/day1-production-image-evidence-20261110/result.json`. Its
Docker config ID is not a Registry release digest.

The first current-source Actions Backend run executed **963 passing tests,
zero failures and zero skips**, but correctly rejected acceptance because the
new Ruff gate tried to create `/app/.ruff_cache` in the non-root container's
read-only source directory. The acceptance runner now sends Ruff's cache to
its writable evidence directory, alongside the existing Mypy/bytecode caches.
The check remains required; no test or quality rule was disabled. The repaired
source must pass a new complete workflow before it is counted as accepted.

The cache-repair push then exposed a timing dependency in the existing
PostgreSQL starvation regression: its direct setup of 101 exhausted jobs used
a three-second ownership lease without the runtime's heartbeat. Slow SQL/fsync
could expire setup ownership before staging. The repository correctly rejected
that attempt; the matching PR matrix passed. This queue-admission test now uses
a scoped fixed repository clock, retaining the full backlog and expired-work
assertions. Production leases and the separate expiration/heartbeat fencing
tests remain unchanged.
The corrected case passed five independent PostgreSQL 17 subprocess runs;
the related operations, runtime-fencing and persistence suite passed **28
cases with zero skips**. SQLite operations/fencing passed 13 cases. The
disposable PostgreSQL container and network were removed; evidence is in
`/tmp/day1-postgres-regression-t377dj6x`.

A disposable **local** lab exercised the real MLflow 3.6 server, PostgreSQL 17
through psycopg2, and source-built MinIO S3. A test-only TLS reverse proxy
implemented Basic writer and GET-only reader authorization. The production
entrypoint and qualification CLI produced these results:

| Scenario | Result |
| --- | --- |
| Valid CA and distinct reader | Exit 0; Run/Registry operations and writer/reader artifact SHA256 passed. Tracking and Registry reader writes were explicitly denied. |
| Invalid reader credentials | Nonzero; `MLFLOW_NOT_QUALIFIED`. |
| Incorrect trust bundle | Nonzero; `MLFLOW_NOT_QUALIFIED`. |

This lab used synthetic revision/digest inputs to exercise validation, not to
certify release provenance. It did not connect to the user's external MLflow or
Databricks. Task-owned lab containers, volumes and network were removed.
The local receipt is `/tmp/day1-mlflow-lab-x5i8_2c9/summary.json`.

The first lab server used the psycopg3 metadata driver. Its Registry
`get_model_version` returned HTTP 400 because PostgreSQL compared the integer
version column with a VARCHAR bind. The qualification correctly rejected it.
Using psycopg2 for this test server resolved that server-side compatibility
issue; the CLI retained the Registry GET check. This does not change the
application's supported `postgresql+psycopg` connection or instruct an operator
to modify an existing external server without its owner's review. The actual
external MLflow must pass the same qualification before Go.

## Outstanding target-environment evidence

The approved external endpoints, private mounts, release Registry digest,
Databricks fixed Delta fixture and independent count/SHA, production capacity,
backup/restore drill and reviewers' sign-off have not been supplied or executed
here. They remain Day-1 Go/No-Go requirements. No external infrastructure was
provisioned, no production migration or model deployment was performed, and no
Go-Live claim is made.

Raw Snapshot acquisition remains separate from the approved ML DatasetVersion.
An automatic Dataset Builder, calibrated Energy/RUL analysis and production
online RUL feasibility remain the separately documented workload packages;
they are not implied by this deployment change.

Use the [Day-1 runbook](../deployment/day1-deployment-runbook.md) for the actual
deployment sequence. The earlier [Actions repair record](actions-failure-fixes-2026-11-09.md)
contains the prior `5ec1c6f` Backend qualification. Current-head CI results are
recorded on PR #1; historical green runs do not qualify later code.
