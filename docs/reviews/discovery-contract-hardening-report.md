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

## H. PR #24 final review closure

The original reviewed head was `73a67722c250de42ae13e60acc4100610673d82e`.
The implementation and tests were committed and verified at
`222c217fdd313fcaf3ee83d0dab8022135de555e`. The master baseline for the PR
remains `65712b49f3d9eb2525941d7f3acb9639dc029ed8`. Sections A–G record the
original review and test runs.

**Finding #8 — FIXED.** A latest Usage Review row with a mismatched stored
tenant could be replayed or used to allocate a globally keyed revision.
`src/infrastructure/persistence/ml_registry.py` now checks that row against
the authorized binding tenant and validates its stored revision before using
it. Migration 0007 uses a PostgreSQL signed `Integer`. The writer now uses
the portable range from 1 through 2,147,483,647 on both databases. A valid
unchanged review at that ceiling can still be replayed. A changed review would
require another revision and is rejected with the existing sanitized
`Conflict` (`STALE_REVISION`, HTTP 409 where mapped). The existing tenant
lock and transaction roll back technical contract and audit writes on rejection.
The binding-wide lookup, schema and historical rows are preserved.

**Nested stored JSON — FIXED.** Excessive nesting beneath the existing size
limits could escape the decoder or canonical serializer as `RecursionError`.
`src/application/governance.py` now returns the existing sanitized 503 when
a requested evaluation row cannot be decoded. Evaluation continuation still
leaves the sentinel undecoded. `src/domain/prediction/catalog.py` maps parsing
and canonicalization recursion to the existing `SchemaMismatch`. Invalid
technical publication fails and historical malformed contracts remain
`incomplete` in Management and Gateway discovery. Canonical serialization and
digests retain their existing algorithms.

### Regression evidence

The new SQL tests cover identical and changed review bytes under inconsistent
tenant identity, both with and without a published technical contract. They
assert that the entire technical contract, usage review and audit state is
unchanged after rejection. They also verify discovery visibility, allocation
at the ceiling, unchanged replay there, exhaustion, lower-bound corruption
on both databases and out-of-range positive rows where SQLite can store them.
Existing tests exercise first publication, ordinary replay, changed review
allocation and foreign-actor rejection. The nested JSON tests exercise decode
and canonicalization failures, an untouched first-page sentinel, sanitized
503 with request ID, rejected publication and incomplete historical metadata.
The focused suite includes the existing valid MLP and EBM prediction paths.

Verification used CPython 3.12.14 on managed Linux, disposable SQLite files
and PostgreSQL 17 on a disposable loopback tmpfs fixture with separate test
schemas. Counts across runs overlap.

| Run | Passed | Failed | Skipped | Result |
| --- | ---: | ---: | ---: | --- |
| Test-first reproduction on reviewed source, SQLite | 1 | 11 | 0 | Expected failures, exit 1, 65 deselected |
| Test-first reproduction on reviewed source, PostgreSQL | 1 | 11 | 0 | Expected failures, exit 1, 65 deselected |
| Adjusted evaluation decoder reproduction on reviewed source | 0 | 1 per database | 0 | Unhandled `RecursionError` reproduced, exit 1 |
| Focused SQLite | 114 | 0 | 0 | PASS, exit 0 |
| Focused PostgreSQL | 114 | 0 | 0 | PASS, exit 0 |
| Full ML and boundary regression, SQLite | 1,523 | 0 | 5 | PASS, exit 0 |
| Local static gates | 15 | 0 | — | PASS, exit 0 |
| Final-head hosted CI and live deployment qualification | — | — | — | NOT VERIFIED |

The test-first replay accepted the inconsistent tenant and invalid revision on both
databases. At the portable ceiling the SQLite writer allocated the next revision
and the PostgreSQL writer raised `Unavailable` after attempting a write. The
nested public contract escaped with `RecursionError`. The evaluation fixture was
then increased to 10,000 levels, still below the size limit, to reliably
reproduce JSON decode recursion on both databases. Baseline evidence is in
`/tmp/pr24-before-*.{log,xml}`. Focused and full results are in
`/tmp/pr24-final-*.{log,xml}` and `/tmp/pr24-full-sqlite.{log,xml}`.

```sh
PY=/workspace/ml-system-prod/.venv/bin/python
# Set ML_TEST_DATABASE_URL to the isolated fixture for postgres. Unset it for sqlite.
db=sqlite
"$PY" -m pytest tests/ml/test_prediction_catalog.py \
  tests/ml/test_governance_discovery.py tests/ml/test_prediction_catalog_e2e.py \
  tests/ml/test_training_discovery.py tests/ml/dataset_builder/test_discovery.py \
  -q --basetemp="/tmp/pr24-final-$db" --junitxml="/tmp/pr24-final-$db.xml"
"$PY" -m pytest tests/ml tests/test_boundaries.py -q -rs \
  --basetemp=/tmp/pr24-full-sqlite --junitxml=/tmp/pr24-full-sqlite.xml
```

The static run includes the architecture boundary check, Pyflakes, CI-scoped
Mypy, ingestion, deployment, Serving, Kubernetes acceptance, Training, EBM and
initialization checks, scoped Ruff lint and formatting, documentation, the
generated manifest and `git diff --check`. Commands and exit codes are in
`/tmp/pr24-static.log`. The registry file is outside the repository formatting
gate. A separate full-file comparison produced the same seven existing Ruff
lint diagnostics and formatting findings on both the reviewed and fixed
versions. Registry formatting changes are outside this closure.

### PR review summary

Finding #8 and the two identified persisted JSON exception boundaries are
closed. The existing authorization, audit, database schema, canonical
serialization and immutable digests are preserved. The earlier seven accepted
dispositions stand. Focused verification passed 114 cases on each database.
The full local SQLite result is listed above. The five skips cover disposable backend qualification, authorized live Databricks, isolated HTTP endpoints, and two real RabbitMQ/Redis receiver tests.

Hosted results from the original review are historical evidence. Final-head
hosted CI and live deployment qualification require separate verification.
The cloud egress policy returned HTTP 403 for the GitHub API required to edit
the PR description. This section records the closure summary for PR #24.
