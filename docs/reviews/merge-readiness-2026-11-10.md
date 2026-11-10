# Source completion and engineering-baseline merge assessment

Date: 2026-11-10. Audited source:
`7a175ad0a2da02fcc70962d297b032a63a58db0d`.
Audited `src` tree: `3ebb9aa3d41bea293a4c2349144c0dd9ea9fd32a`.

The current PR's source modules are implemented and integrated. Three independent
read-only reviews found no remaining concrete runtime stub, broken wiring or
source blocker in the selected CPU ML, ingestion and deployment scope. This is
a scoped engineering review, not a claim that every future ML work package is
implemented or that production qualification is complete.

## Completed source scope

| Area | Reviewed implementation |
| --- | --- |
| Generic ingress | Provider-neutral `BatchSourceAdapter`, explicit Registry selection, approved immutable Sources, tenant/actor authorization and durable SQL Jobs. |
| File and Databricks acquisition | Existing File adapter and official SQL Connector/OAuth M2M adapter; approved templates, fixed Delta SQL, bound parameters, explicit codec, bounded subprocess/fetch/staging and cancellation/cleanup. |
| Publication and recovery | Private staging, durable publication intent, owner/lease fencing, immutable identical-content retries, conflicting-content rejection, bounded reconciliation and fair cleanup. |
| CPU MLP/EBM training | Controlled Recipes, immutable FeatureSnapshots, train-only normalization, CNN/loss/sampler, real CPU child training, safe epoch resume and immutable neural bundles. |
| Evaluation/governance/Serving | Exact-bundle evaluation, fail-closed quality policy, manual Approval, fixed-version verified-byte runtime, golden warm-up, strict prediction contracts, fenced rollout/rollback and bounded physical execution. |
| Composition and deployment | Opt-in factories, complete runtime roles, migrations, private per-role files, supported PostgreSQL driver, scoped reader credentials/CA, unified image, offline preflight and retained MLflow qualification. |

No Databricks-specific branch or SDK dependency was found in Domain,
Application, Training or Serving. `...` in ports denotes Protocol declarations;
the reviewed `pass` statements are exception markers or intentional cleanup/
failure handling, not missing concrete implementations.

## Verification

- Audited-source [push](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42742534571)
  and [PR](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42742540971)
  workflows passed all five required jobs and cleanup. Both Backend reports:
  **963 passed, zero failures/errors/skips**, Python 3.12.16 / Torch 2.6 CPU.
- Actual unified-image/non-root entrypoint checks passed; each SQLite/PostgreSQL
  matrix recorded 971 passed and two separate opt-in skips. The protected
  Databricks live job was not invoked.
- Fresh source review checks: Ingestion Ruff/format/strict Mypy and **143** focused
  acquisition, recovery, operations, fencing and snapshot cases passed, zero
  skips. Deployment checks, boundaries and **107** focused cases also passed.
  These suites overlap existing coverage and are not added into a new total.
- The post-review changes refresh status/merge-scope documents only. Required CI
  must still pass for the exact PR head before merge; audited-source evidence
  does not automatically qualify a later code change.

## Remaining work is explicitly retained

DBX-09 has executable live-test code but no approved real Warehouse fixture,
secret mounts or independent expected count/SHA result. The user's external
MLflow, trusted Registry release provenance, capacity/SLO/security, site
ingress and backup/restore drills also remain unqualified. The local MLflow/
PostgreSQL/S3 TLS lab uses a test-only authorization gateway.

Automatic Raw-to-Feature/Dataset Builder, Databricks Remote Executor,
CalibrationSnapshot, calibrated Energy/RUL, Batch Analysis/Report Store and
as-of/rolling-origin validation remain separate unimplemented work packages.
Raw acquisition does not produce a trainable DatasetVersion automatically.
Real Notebook numerical parity, representative data and product-owned quality
thresholds are still required for model acceptance.

## Integration decision and release boundary

The earlier delivery instructions held the PR Draft until external acceptance.
The maintainer's latest direction is to merge the completed source modules
after checking remaining engineering details. This assessment therefore allows
integration as a **completed engineering baseline**, while preserving the
original external/model/release requirements as uncompleted follow-up gates.
It does not claim DBX-09, formal Tech Lead/ADR approval or production acceptance.

The source-completion condition is satisfied within this PR's declared scope.
Merge only after current-head required checks pass, using a merge commit to
retain the individual implementation commits. Do not bypass repository
protection rules or trigger production deployment as part of this decision.
Production acceptance remains false until the
[Day-1 checklist](../deployment/day1-deployment-runbook.md) is executed and
signed off with the approved environment.

The obsolete Databricks/Backend statements in the implementation status indexes
were corrected; dated execution reports remain historical evidence. The original
Notebook and unrelated untracked design proposals remain outside this PR.
