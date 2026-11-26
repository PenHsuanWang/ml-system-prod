# Phase 0 + WP1 delivery report

## A. Baseline and scope

- Branch feature/prediction-api-guide, HEAD d0bca4fbcdd967f342e63721ea4f0dc439088a22.
- Initial worktree and index clean. All changes below are this session's uncommitted working-tree changes.
- Remote master a5b1767 and feature d0bca4f were confirmed through read-only Git operations.
- No applicable AGENTS.md found. The named Master Specification file was absent from local and fetched trees, then the user supplied its content inline. Its architecture guardrails and WP1 stop gate were reviewed.
- Authorized scope is Phase 0 + WP1. The complete lifecycle was investigated through routers, application policies, DTOs, repository implementations, workload contracts and tests.
- Existing Python 3.12.14 virtual environment reused at /workspace/ml-system-prod/.venv. FastAPI 0.165.0, Pydantic 2.14.4, HTTPX 0.28.1, Pytest 9.2.0, Torch 2.6.0+cpu. Docker 29.9.2 and Compose 2.40.3 available. No PostgreSQL test resource selected or accessed.

## B. Findings

The [inventory and roadmap](../plans/self-service-api-inventory.md) covers Identity, Catalog, sources,
ingestion, snapshots, dataset/feature preparation, training, registry/evaluation/approval,
deployment and prediction, including profile-dependent registration.

Verified prediction behavior is as follows.

| Contract | Observed behavior |
| --- | --- |
| Successful upstream response | response_model alone was bypassed by JSONResponse. Previously a malformed prediction list could be returned with 200 after identity checks. Now strict JSON-model validation runs before forwarding the original body. |
| MLP / EBM success | Actual HTTP fixture bodies validate against the published DTO, with registered name/version, input schema, manifest/spec digest, generation and request ID. Real PyTorch serving regression is also run. |
| 401 / 403 / 404 | Invalid, expired, revoked and missing credential cases, missing ml:predict, cross-tenant route and unknown endpoint verified through isolated Identity/SQL-route fixtures. Identity fixture rejection is not a live Mongo session qualification. |
| 413 | 8 MiB limit enforced by existing middleware; body is code/message/retryable and omits request_id. X-Request-ID remains available in the response header. |
| 422 | Invalid payload sanitized; shared handler creates a different body request ID from the tracing header. Documented and deferred for a separate shared-handler review. |
| 429 / 504 | Real BoundedPredictionExecutor deadline leaves physical slot occupied; subsequent request gets CAPACITY_EXCEEDED. Existing ML handlers and Gateway carry both JSON failures to the client. |
| 503 | HTTP ReadTimeout maps to DEPENDENCY_UNAVAILABLE. Strictly invalid upstream success also returns that existing sanitized 503. Readiness returns ready=false on a dependency failure. |
| Error pass-through | Existing non-success JSON response body/status preserved. Non-JSON upstream responses map to existing 503. A new shared error format was not introduced. |
| Reverse proxy and docs | Root redirect preserves root_path; Swagger, ReDoc and OpenAPI accessible through /inference prefix. Explicit prediction operation ID preserves the baseline generated ID. |

## C. Implemented changes

| Files | Change |
| --- | --- |
| bootstrap/ml_gateway.py | English summaries, stable existing operation IDs, strict successful upstream JSON validation using the existing DTO and error path. |
| src/api/prediction_docs.py | English first-use/auth/permissions, response and error semantics, retry/correlation guidance, synthetic MLP/EBM inputs and provenance field descriptions. |
| src/api/ml_schemas.py, src/api/ebm_schemas.py | Prediction field descriptions without changed request defaults/ranges/extra-field policy. |
| tests/ml/test_prediction_gateway_docs.py | Actual HTTP success and errors, auth/tenant, proxy, strict response failure, body limit, real bounded executor and health contracts. |
| tests/ml/test_serving_runtime.py | Existing mock success now supplies request_id/input_schema_version already supplied by real Serving; retains fencing assertions. |
| docs/plans/self-service-api-inventory.md | Lifecycle inventory, gaps and phased implementation/rollback plan based on the supplied master specification. |
| docs/standards/api-design-standard.md | Common English documentation and compatibility standard. |
| docs/plans/prediction-endpoint-discovery.md | Explicit current implementation/WP1 vs proposed WP2/legacy/persistence/acceptance/deferred scope. |
| docs/README.md | Links to the foundation deliverables. |
| docs/reviews/wp1-api-foundation-report.md | This report. |
| FILE-MANIFEST.json | Refreshed hashes, including the three new documentation files in the review working tree. |

API adapters and bootstrap remain the only runtime change locations. Domain/application,
management handlers, database schema and execution state machines have no changes.
Successful responses retain their original JSON and existing identity/route checks.
Malformed success rejection is the demonstrated minimal runtime fix. Secret values and external
services were not used. The new tests fail on invalid outputs without echoing upstream details.

## D. Verification

All commands run from /workspace/ml-prediction-guide on the baseline plus this local diff.
The commands below use the existing virtual environment directly.

| Command | Status | Result / evidence |
| --- | --- | --- |
| /workspace/ml-system-prod/.venv/bin/python -m pytest tests/ml/test_prediction_gateway_docs.py tests/ml/test_serving_runtime.py tests/test_boundaries.py -q | FAILED, corrected | First added fixture returned a synchronous TestClient stream through AsyncClient; 4 failed/200 passed. Rewrapped the buffered response in the test transport. The next run passed 204 cases. /tmp/wp1-focused.log |
| /workspace/ml-system-prod/.venv/bin/python -m pytest tests/ml/test_prediction_gateway_docs.py tests/test_boundaries.py -q | PASSED | 176 passed; no skipped tests. /tmp/wp1-focused-final.log |
| /workspace/ml-system-prod/.venv/bin/python -m pytest tests/ml/test_prediction_gateway_docs.py tests/ml/test_pytorch_e2e.py tests/ml/ebm/test_prediction_boundary.py -q | PASSED | 31 passed before final health-only test addition. /tmp/wp1-http.log |
| /workspace/ml-system-prod/.venv/bin/python -m pytest tests/ml tests/test_boundaries.py -q -rs --junitxml=/tmp/wp1-regression.xml | PASSED | 1377 passed, 0 failed, 5 skipped in 75.11s; exit 0. /tmp/wp1-regression.log. ML_TEST_DATABASE_URL, ML_TEST_MANAGEMENT_URL, ML_TEST_PREDICTION_URL, ML_TEST_AMQP_URL, ML_TEST_REDIS_URL removed from this command environment. |
| /workspace/ml-system-prod/.venv/bin/python -m pyflakes bootstrap src tests/ml | PASSED | No findings. |
| /workspace/ml-system-prod/.venv/bin/python -m ruff check src/api/prediction_docs.py src/api/ebm_schemas.py tests/ml/test_prediction_gateway_docs.py | PASSED | Initial line-length findings corrected before the final check. |
| /workspace/ml-system-prod/.venv/bin/python -m ruff format --check src/api/prediction_docs.py src/api/ebm_schemas.py tests/ml/test_prediction_gateway_docs.py | PASSED | 3 files formatted. |
| /workspace/ml-system-prod/.venv/bin/python scripts/check_boundaries.py | PASSED | Dependency boundaries passed. |
| /workspace/ml-system-prod/.venv/bin/python scripts/check_serving_runtime.py | PASSED | Existing Ruff and format scopes passed. |
| /workspace/ml-system-prod/.venv/bin/python -m mypy --follow-imports=skip src/domain/ml.py src/domain/training src/domain/deployments src/domain/model_registry src/domain/prediction src/domain/datasets src/application/training src/application/deployments src/application/model_registry src/application/prediction src/application/datasets src/application/ml_services.py | PASSED | CI-defined scope, 39 source files. |
| /workspace/ml-system-prod/.venv/bin/python -m mypy --follow-imports=skip src/api/prediction_docs.py | PASSED | One modified DTO source. |
| PYTHONPATH=. /workspace/ml-system-prod/.venv/bin/python /tmp/wp1-review-docs.py --write; PYTHONPATH=. /workspace/ml-system-prod/.venv/bin/python /tmp/wp1-review-docs.py | PASSED | 132 documents and 612 manifest entries. Existing docs link checker and manifest hashing applied to tracked files plus explicitly named new review documents. Index unchanged. The stock docs/manifest CLIs enumerate the Git index only, so this working-tree review invokes the same link_errors/manifest hashing with the three explicitly named untracked documents included. |
| git diff --check; git diff --cached --stat; git status --short --untracked-files=all | PASSED | Final diff review and stop-gate verification. |
| Full Compose/Kubernetes/enterprise dependency qualification | NOT_EXECUTED | Outside this session's WP1 gate. Local ML suite retains separately reported opt-in skips. |

The local HTTP fixture exercises the real Gateway, ML tracing/error middleware, strict response
boundary and bounded executor. Identity/routes and prediction values are controlled synthetic
fixtures. It does not claim an independent production service deployment or live EBM model load.
The PyTorch test uses the existing independent fixed-serving path.

## E. Remaining work

- WP2 follows manual WP1 review on an explicitly authorized separate branch. Its two GET endpoints remain proposed.
- Model contract persistence, verified legacy backfill and reviewed business descriptions are WP2 planning work.
- Dataset lists/build orchestration, recipes/job discovery, registry/deployment transparency and production UX belong to WP3–WP7.
- Aligning the shared 422 body/header IDs needs a separate shared error-handler change.
- OpenAPI is an interface contract, not production readiness evidence. Live dependency/session, self-hosted docs assets, target proxy/TLS/network and full-stack enterprise behavior need their respective qualification.
- No WP1 release blocker is currently identified, subject to the final verification results below. production_accepted remains false.

## F. Delivery status

COMPLETE for Phase 0 + WP1. All required local WP1 gates passed.
Five existing environment-dependent integration tests remain skipped and are not counted as
executed passes. This phase's HTTP success/error verification is fixture-based.
No WP2 router/service/storage or WP3–WP7 runtime implementation was added.
At the initial WP1 handoff, changes were left uncommitted for local review.
The user subsequently authorized continued development and sequential commits. This WP1
change set is now prepared for a local commit on the same feature branch.
A repeated focused run passed 176 cases and the boundary check passed before committing.
The overall self-service platform remains partial.
