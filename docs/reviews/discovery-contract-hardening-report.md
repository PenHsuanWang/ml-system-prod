# Discovery contract hardening review

## A. Repository state

Base branch `master`, commit `65712b49f3d9eb2525941d7f3acb9639dc029ed8`
(merge of PR #23). Feature branch `fix/discovery-contract-hardening`.
All seven approved defects were reproduced on that merge's source after
checking the original review base `e6c257e`.
Implementation and tests are committed at
`33819c138e3f00f223b4f153a34483fcda4e3747`. The delivery commit records this
report and regenerates the manifest. Both commits are local.

## B. Finding disposition

| Finding | Status | Root cause or rationale | Changed files | Regression evidence |
| --- | --- | --- | --- | --- |
| #1 | REJECTED | Prediction verifies authoritative deployment identity. Omitting invalid rows would weaken integrity and change cursor continuation. | | Prediction identity and routing suite. |
| #2 | FIXED | Evaluation projection read the extra SQL row before the API sliced the page. | Application governance and governance tests. | A valid page succeeds with a malformed sentinel. Requesting the malformed row returns 503. A valid last page ends continuation. |
| #3 | FIXED | Pydantic's persisted response validation raised outside the domain error handler. | Governance API and governance tests. | Corrupt approval metadata returns sanitized 503 with the matching request ID. A bad query still returns 422. |
| #4 | REJECTED | Technical metadata and reviewed usage have separate verification and an incomplete status. | | Existing Management contract and Gateway discovery suites. |
| #5 | REJECTED | Job read authority follows tenant and owner or `ml:jobs:read_all`. | | Training discovery visibility and ownership tests. |
| #6 | FIXED | Domain validation omitted schema version strings, units and output semantics. | Prediction domain and prediction/governance tests. | Strict field matrix, real MLP and EBM publication, and historical digest-valid invalid records remain incomplete. |
| #7 | INVESTIGATED | Binding authorization and the existing technical-row tenant check protect publication, including deliberate tenant mismatch in storage. | | Disposable mixed-tenant technical contract test on both databases. |
| #8 | INVESTIGATED | Latest review lookup uses the globally keyed binding and treats a corrupt tenant row as authoritative for idempotency and allocation. | | Disposable mixed-tenant usage and integer-boundary experiments on both databases. |
| #9 | REJECTED | Publication checks current source visibility even when an earlier intent can be replayed. | | WP6 revoked-grant and lineage publication tests. |
| #10 | REJECTED | ADR-024 requires raw input owner visibility or the established operator grant. | | Dataset lineage visibility and cross-owner tests. |
| #11 | FIXED | Cursor truthiness conflated absent and empty service inputs. | Application prediction catalog and prediction tests. | Empty string fails through the existing cursor contract. Signed pagination, expiry, scope and limit binding pass. |
| #12 | FIXED | Finiteness conversion of oversized integers could reject the entire metrics page. | Application governance and governance tests. | Oversized integers and invalid types are filtered. Valid scalar types, sibling metrics, report digest and decision are preserved. |
| #13 | DEFERRED | Empty history pages can occur at valid offsets. A supported route deletion race has not been demonstrated. | | Deployment discovery and pagination regression. |
| #14 | FIXED | Snapshot-list reconstruction exposed client errors or escaped errors for persisted identity and JSON corruption. | SQL ingestion discovery and dataset discovery tests. | Identity, malformed JSON, schema, timestamp overflow and nested JSON cases return safe 503. Valid continuation and request validation pass. |
| #15 | FIXED | Contract limits lacked strict value typing. | Prediction domain and prediction tests. | Integers pass. Boolean, integral float and string values fail. Invalid publication creates no contract or review. |

## C. Implementation

Evaluation projection touches only the requested rows and retains the sentinel for
the existing API continuation calculation. The shared Governance page validates
only those returned rows and translates its specific Pydantic validation error.
Metric projection keeps the existing allowlist and retains JSON scalar types.
Oversized integers are filtered at the finiteness check. Persisted reports remain
the evidence for approval and retain their original digests.

Prediction validates the existing wire field types in the framework-free domain.
Canonical serialization, digest computation and binding identity use the
existing code. The previously malformed historical test fixture now writes its
historical digest and document directly to disposable SQL, so it represents a
record from a version that accepted that document. Management and Gateway still
return incomplete metadata through their existing paths. The cursor fix sends
every supplied value through the same signed decoder.

Snapshot discovery translates reconstruction failures at `list_snapshots`.
It uses the same identity checks and SQL scope. Dataset lineage and publication
continue to call the original verifier with its established exception semantics.
The collection still fetches and verifies its sentinel. A corrupt row therefore
fails the snapshot read as a whole.

## D. Test results

Runtime verification used CPython 3.12.14 on managed Linux. SQLite used
disposable files. PostgreSQL 17 ran in a disposable loopback tmpfs fixture with
per-test schemas. The ingestion repository unit fixture in the related suite
explicitly selects SQLite. PostgreSQL coverage for ingestion snapshot listing
comes from the real SQL/HTTP DatasetBuilder discovery fixture. The database
variants are overlapping runs.

| Verification | Passed | Failed | Skipped | Status |
| --- | ---: | ---: | ---: | --- |
| Test-first SQLite selection on base source | 16 | 21 | 0 | Defects reproduced, exit 1, 55 deselected |
| Related contracts, SQLite | 148 | 0 | 0 | PASS |
| Related contracts, PostgreSQL | 148 | 0 | 0 | PASS |
| Registry investigation, SQLite | 3 | 0 | 0 | PASS, observed corruption behavior |
| Registry investigation, PostgreSQL | 3 | 0 | 0 | PASS, observed corruption behavior |
| Full ML and boundaries, SQLite | 1,512 | 0 | 5 | PASS |
| Local static gates | 15 | 0 | — | PASS |
| Remote CI and live deployment qualification | — | — | — | NOT EXECUTED |

```sh
PY=/workspace/ml-system-prod/.venv/bin/python
# SQLite leaves ML_TEST_DATABASE_URL unset. Set it to the disposable fixture for PostgreSQL.
"$PY" -m pytest tests/ml/test_governance_discovery.py \
  tests/ml/test_prediction_catalog.py tests/ml/test_prediction_catalog_e2e.py \
  tests/ml/test_training_discovery.py tests/ml/dataset_builder/test_discovery.py \
  tests/ml/dataset_builder/test_build_jobs.py tests/ml/ingestion/test_persistence.py \
  tests/ml/ingestion/test_api.py -q
"$PY" -m pytest -p tests.ml.conftest /tmp/test_contract_registry_investigation.py -q
env -u ML_TEST_DATABASE_URL "$PY" -m pytest tests/ml tests/test_boundaries.py -q
```

Logs, exact pytest commands with evidence paths, status and JUnit data use
`/tmp/contract-hardening-*`. The static log records commands and exit statuses
for architecture boundaries, Pyflakes, CI-scoped Mypy, scoped Ruff lint and
formatting, ingestion/deployment/Serving/Training/EBM/Kubernetes-acceptance/init
static gates, docs, manifest and Git whitespace validation.

## E. Registry investigation

**Finding #7.** The tenant-scoped model binding is authorized before either
contract or review lookup. `model_version_bindings.id` is globally unique
and the technical contract references that key. Changing a technical row's
`tenant_id` directly in SQL causes publication by the authorized owner to
fail with the existing immutable-contract conflict. A foreign actor receives
`NotFound`, and the owner sees incomplete discovery. This finding provides
no basis for a separate technical-contract write correction.

**Finding #8.** Migration 0007 keys the usage table on `(binding_id, revision)`.
Its tenant column is required and can disagree with the referenced binding
after storage corruption. The writer's latest-row query and its idempotency
return use binding and revision without checking that tenant. In each
disposable database, publish revision 1, insert a digest-valid revision 8
with a synthetic wrong tenant, then replay that review through the normal
writer. The call returns 8 and the authorized discovery views still see
revision 1. A subsequent different review is assigned revision 9. The
foreign tenant sees no binding or endpoint. In another fixture the corrupt
revision was placed at the database's integer maximum. PostgreSQL produces
the existing sanitized `Unavailable` database error. SQLite's binder raises
an unwrapped `OverflowError` that escapes the writer. The prior visible
revision survives both failures. Publication uses the operator CLI.

A separate narrow hardening change is justified for #8. Check the tenant
of the latest authoritative row before both idempotency and allocation,
and fail with an established safe error. Filtering only by tenant would
leave corrupt rows occupying globally keyed revision numbers. Add tests
for same/different review bytes, revision exhaustion, and authorized
discovery. The current patch keeps the existing registry SQL and schema
for separate review.

## F. Remaining limitations

The change qualifies the exercised local application, database and API
contracts. The five full-suite skips cover the disposable Backend stack,
approved live Databricks, isolated Management/Prediction HTTP endpoints,
and two real RabbitMQ/Redis receiver cases. Both database variants also
reported the existing Starlette TestClient deprecation warning. Production
and enterprise qualification remain outside the approved scope.

## G. Final diff summary

- Production changes are in `src/api/governance_discovery.py`,
  `src/application/governance.py`, `src/application/prediction/catalog.py`,
  `src/domain/prediction/catalog.py` and
  `src/infrastructure/ingestion/persistence/ingestion_jobs.py`.
- Regression tests are in `tests/ml/test_governance_discovery.py`,
  `tests/ml/test_prediction_catalog.py` and
  `tests/ml/dataset_builder/test_discovery.py`.
- The implementation commit adds 336 lines and removes 41, including the
  generated manifest. This report and its manifest digest form the delivery
  commit. Production changes are limited to the seven approved findings.

## PR description

**fix: harden discovery pagination and public contract validation**

### Summary

Correct Evaluation pagination, stored-response errors and public contract
validation while retaining existing authorization and service boundaries.

### Changes

- Keep the evaluation sentinel for continuation and project only visible page rows.
- Map stored Governance and snapshot decoding/identity errors to the existing safe 503.
- Validate strict limit types, public schema versions, units and output semantics
  before immutable contract publication.
- Reject explicitly empty cursors and filter unrepresentable public metrics.

### Non-goals

Reuse the existing authorization, publication-time source visibility, registry,
database schema, infrastructure and deployment contracts. Findings #7 and #8
are investigation evidence for separate review.

### Verification

SQLite and PostgreSQL related suites passed 148 each. The full SQLite ML
regression passed 1,512 with five external-service skips. The disposable
registry investigation passed three cases on each database. The architecture,
quality, documentation and manifest gates passed.
