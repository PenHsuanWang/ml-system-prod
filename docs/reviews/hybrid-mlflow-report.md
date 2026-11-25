# Hybrid external MLflow engineering report

## A. Repository baseline

Baseline master e1dc4234c32c6586b31aed5a73d977abaa13348f.
Branch feature/hybrid-external-mlflow, work package B.
Final implementation source 4f053be31d17f6d5f181a7e82e1fe6a1fead1e3f. Documentation/evidence commits follow.
Independent of the discovery and corporate-build branches.

## B. Requirement traceability

| Requirement | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| 6.1 preserve existing profiles | Additive standalone hybrid model | Existing six init render variants; full regression | INTEGRATION_TESTED |
| 6.2 local infrastructure/external tracking | No bundled MLflow/MLflow DB/MinIO; four private local services | Actual Compose rendering, explicit topology preflight | INTEGRATION_TESTED |
| Migration order/shared namespace/storage | Health dependencies; migrate completion; gateway service:control | Model negative tests and real remote lifecycle | COMPOSE_QUALIFIED |
| 6.3 matching HTTPS URIs/auth/CA | Existing strict production entrypoint and one external URI | Existing loader negatives; actual TLS/auth fixture | COMPOSE_QUALIFIED |
| 6.4 role credential isolation | Existing private role files and reader-only child allowlist | Loader regression and dependency reader write denial | COMPOSE_QUALIFIED |
| 6.5 full lifecycle | Existing SQL/outbox/MQ/training/registry/evaluation/approval/control/serving/gateway | Hosted Backend job's dedicated hybrid step | COMPOSE_QUALIFIED |
| 6.6 dependency qualification | Existing qualify-mlflow CLI | Writer run/registry/upload/download and reader denial | COMPOSE_QUALIFIED |
| 6.7 setup/cutover/rollback | New example + existing Day-1 guide | Document/link checks; no runtime switching API | IMPLEMENTED |
| Existing contracts/authorization | No protected core rewrite or schema change | 1329 local passes, 108 focused deployment passes | UNIT_TESTED |
| Enterprise MLflow | No company endpoint or credentials used | Not executed | EXTERNAL_ENVIRONMENT_NOT_VERIFIED |
| New K8s runtime | No topology modification | No dynamic cluster execution | DEFERRED |

## C. Changed files

A means added, M modified; no deleted files. Full inventory follows.

| File | Type | Purpose | Architecture impact |
| --- | --- | --- | --- |
| .env.ml.hybrid.example | A | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| .github/workflows/ml-ci.yml | M | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| FILE-MANIFEST.json | M | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| README.md | M | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| bootstrap/deployment_cli.py | M | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| compose.ml.hybrid-mlflow.yaml | A | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| docs/configuration-reference.md | M | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/deployment/day1-deployment-runbook.md | M | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/boundaries.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/focused-delivery.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/focused-final.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/init-compose.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/init-quality.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/k8s-contracts.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/k8s-static.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/local-attempt-01.json | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/local-attempt-02.json | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/local-attempt-03.json | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/local-attempt-04.json | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/qualification.json | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/quality-delivery.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/quality-final.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/regression-delivery.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/remote-success-steps.json | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/evidence/hybrid-mlflow-20261126/root-ownership.txt | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/implementation-status.md | M | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/plans/hybrid-external-mlflow.md | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| docs/reviews/hybrid-mlflow-report.md | A | Recorded qualification and review | Existing lifecycle/security contracts preserved |
| scripts/check_deployment.py | M | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| scripts/verify_hybrid_mlflow.py | A | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| scripts/verify_ml_acceptance.py | M | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| tests/ml/integration/test_backend_stack.py | M | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| tests/ml/test_hybrid_compose.py | A | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |
| tests/ml/test_hybrid_deployment.py | A | Hybrid topology, private configuration, qualification and contract checks | Existing lifecycle/security contracts preserved |

## D. Architecture impact

~~~mermaid
flowchart LR
  Client --> Management
  Management --> SQL[(Local PostgreSQL)]
  SQL --> Relay --> MQ[Local RabbitMQ]
  MQ --> Supervisor
  Supervisor --> Tracking[External HTTPS MLflow]
  Supervisor --> Redis[Local technical snapshots]
  SQL --> Control
  Control --> Tracking
  Control --> Serving[Existing fixed-version serving child]
  Client --> Gateway --> Serving
  Identity[(Local MongoDB)] --> Management
  Identity --> Gateway
~~~

The standalone model reuses existing strict production-role contracts for an
isolated hybrid development topology. prod settings enforce pins, verified TLS
and private identity rules; they do not imply production qualification. No reset/
override Compose features or new minimum beyond ordinary Compose v2 are required.
Existing reference/external production models and init wizard modes are unchanged.
No SourceDefinition, training state machine, outbox, ACK, lease/fencing, registry,
dataset, serving launcher, Kubernetes topology or database schema changes exist.

## E. Security review

Per-infrastructure private env/config files remain separate from per-application
role JSON. Infrastructure has no host ports. Management/migrate/relay/gateway
receive no MLflow credentials. Supervisor gets writer only; Control and Operator
hold reviewed writer/reader identities; Serving inherits only the existing reader
allowlist and approved CA. No full parent environment is forwarded. External
tracking requires consistent HTTPS URIs, explicit auth, valid CA and TLS checks;
URL credentials and insecure fallback are rejected by existing contracts.

The dedicated fixture uses fresh projects, synthetic data/credentials, verified
TLS with explicit allowed hosts, unauthenticated denial and reader write-denial.
Its private files are excluded from artifacts. Public lifecycle logs redact known
fixture secrets and bearer/JWT tokens. Root-created directories and files now
belong to the intended nonroot UID. No existing user configuration, host trust,
Docker daemon configuration or production service was changed.

## F. Validation results

See [machine-readable evidence](../evidence/hybrid-mlflow-20261126/qualification.json)
for commands, environment, source, counts, exit status and log hashes. Local full
regression passed 1329/0/5; focused deployment/loader/qualification tests passed
108/0/0; six existing initialization Compose variants rendered successfully.
Ruff, formatting, mypy, Pyflakes, boundary and documentation checks pass.

The real [Backend CI job](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/45681888981/job/139446621928)
on ac4d18f passed both the unchanged full Backend regression and the dedicated
hybrid external-style MLflow qualification step. Its fixture is outside the
application dependency graph and exercises real HTTP Identity, SQL/outbox,
RabbitMQ dispatch, training, authenticated run/registry/artifact writes, evaluation,
manual approval, controller, reader artifact materialization, fixed serving,
gateway predictions, rollout/rollback and token revocation. The existing CLI
additionally checks reader tracking/registry write denial and artifact integrity.
Hosted JUnit counts/artifacts were not downloaded through the restricted API;
the observed successful job/step status is retained explicitly. The artifact is
ml-backend-acceptance, including var/hybrid-ci/qualification.json and lifecycle.xml.

Local full-container attempts were blocked by the Cloud vfs disk budget. Verified
TLS/auth and a direct isolated migration succeeded locally, but those partial
attempts are not classified as end-to-end passes. Earlier fixture TLS server/host
policy mistakes were corrected without disabling security. New Compose rendering
cases run in Docker-enabled host contracts; all their assertions remain, while
pure preflight cases still run in the Backend image. A final fixture-only ownership
fix was checked by a real root-to-nonroot read test; the prior complete
qualification and the exact final source check are distinguished in the evidence. Existing tests or CI assertions were not weakened.

## G. Remaining risks and blockers

Enterprise TLS, auth, artifact proxy, least privilege, capacity and network remain
EXTERNAL_ENVIRONMENT_NOT_VERIFIED. The fixture cannot qualify a company's MLflow.
External artifact access assumes the approved MLflow artifact proxy contract.
No runtime endpoint switch or registry migration is implemented. Existing bindings
refer to their original registry; use fresh coordinated state or a separately
qualified migration for cutover. Recreate containers to reload environment; a
restart does not apply changed configuration. Rollback must restore endpoint,
state, credentials and release together. Dynamic K8s acceptance remains deferred.
production_accepted remains false.

## H. Merge recommendation

READY FOR REVIEW for the engineering change set, subject to the final observed
CI status. This does not authorize merge or production deployment.
Proposed PR title: feat(deployment): support hybrid external MLflow configuration.
This report and the updated Day-1 operator walkthrough supply the PR description.

The 40-resource Kubernetes foundation rendering and K8s acceptance static gate
also pass without accessing a cluster. Dynamic acceptance remains DEFERRED.
Concurrent integration of these independent branches must combine shared
documentation/CI/host-qualification entries and regenerate FILE-MANIFEST.json.
