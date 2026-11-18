# Governed Kubernetes acceptance — 2026-11-18

Baseline: merged PR #10, master `336615e`.
Feature: `feature/k8s-acceptance`, Draft PR #11.

## Result

**K8S_ENGINEERING_ACCEPTANCE_PASSED** on implementation source
`fe10413ba4b14735d7b6d4b07a9c98df1f068a62`: all **12 ordered checks**, including
all **7 real Training fault cases**, passed in one fresh CI cluster/run without
continuation. All four lifecycle fits and seven fault fits have one attempt and
confirmed stop evidence. Final broker ready/unacked are both zero.

Independent **FUNCTIONAL_BACKEND_ACCEPTED**: **1169 passed, zero
failures/errors/skips, no blockers** in the complete real-service Compose stack.
The original K8s/Compose reports and verified artifact ZIP digests are retained
in [qualification evidence](../evidence/k8s-acceptance-2026-11-18/qualification.json),
[Kubernetes receipt](../evidence/k8s-acceptance-2026-11-18/kubernetes.json) and
[Compose receipt](../evidence/k8s-acceptance-2026-11-18/compose.json).

Implementation CI: [K8s #6](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44231292649),
[ML/Compose #94](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44231292637),
[Foundation #19](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44231292529).
Final documentation/evidence-head CI is recorded in the PR description.
Production acceptance remains false.

## Scope

Work package 5 combines Pod Training and native Serving in the same disposable
`ml-k8s-acceptance` namespace. Fits use authenticated Management HTTP, SQL Outbox,
Relay, RabbitMQ/Taskiq, the Pod guardian, MLflow and independent evaluation.
Approvals/deployments/rollback use HTTP and predictions use Gateway. Only scoped
synthetic accounts/datasets/raw inputs are fixtures; the operator never starts a
training supervisor or edits SQL job/owner/route/terminal state.

The production entrypoint and verified MLflow TLS remain enabled, including the
strict model-environment setting. The isolated Acceptance overlay alone selects
the named synthetic engineering EBM policy and bounded fault-test time budgets.
Engineering reviewer approvals do not authorize a real production installation.

There are no changes under `src/` or `bootstrap/`, no schema migration, algorithm,
feature/loss, seed, dataset/artifact/checkpoint, approval/fencing or global Serving
capacity change. Original Compose configuration and the default local adapter
remain unchanged. Dev fixtures/tools are added only to a bounded tools image;
production application dependencies remain unchanged.

## Required live gate

The fresh `Kubernetes governed acceptance` CI job builds the actual source-pinned
production/tools images and pinned infrastructure, starts Minikube, locks real
containerd manifest digests and runs all twelve ordered checks from zero. It does
not use the local continuation option. The gate includes:

| Area | Required observation |
| --- | --- |
| Inputs | Existing ingestion adapters publish synthetic raw snapshots; DatasetBuilder verifies its receipt and immutable dataset digest. Live Warehouse access is separate. |
| Admission | Repeat HTTP idempotency key gives the same job and one attempt; real Worker fits/evaluates it. |
| Governance | Foreign tenant reads fail; a viewer cannot approve; missing approval cannot deploy; real inverted-label quality rejection cannot be approved. |
| MLP | Explicit HTTP approval → native activation → Gateway provenance. |
| Recovery | Normal Serving Pod deletion, whole ACTIVE native Deployment reconstruction and shared Control/Gateway Pod restart preserve business route/generation/approval/spec. |
| Failed candidate | A new candidate copies intentionally invalid reader credentials. The shared source Secret is restored immediately. The candidate fails at the existing progress deadline, is fenced/retired, and ACTIVE routing remains usable. |
| Rollout | Approved update retires the old physical runtime; rollback creates a new generation and confirms previous-runtime release. |
| EBM | Raw input/builder/dataset → real Worker → exact MLflow run/version/manifest/evaluation → explicit approval → native inference, including finite output/latent contract. |
| Capacity | Two approved endpoints occupy the existing global reservations; a third is rejected. |
| Training faults | All seven real Training scenarios execute while both endpoints remain deployed; both endpoints predict successfully afterward. |

The seven Training scenarios are MLP, EBM, cancel, independent wall deadline,
Worker crash → actual SQL owner change → same-fit Control recovery → redelivery
ACK, Redis outage with terminal SQL/unacked delivery, and normal busy Supervisor
Pod drain/replacement. Each job must have one attempt, a matching confirmed stop
receipt, correct source/image and a technical snapshot; final ready/unacked are
both zero. Partial runs, duplicate/missing cases, mixed sources, missing stop/ACK
evidence or a missing Control owner change cannot pass the evidence validator.

## Local executed evidence and corrections

- **62 K8s boundary/evidence/checkpoint tests passed.**
- **122 cases passed in the actual final tools image**, read-only root, no source
  mount, zero skipped; source `fe10413ba4b14735d7b6d4b07a9c98df1f068a62`.
- The retained local stack used runtime/tools source `61b5d51`. Earlier incomplete
  receipts are preserved. The first-model recovery checkpoint resumed only after
  checking identical runtime/tools, fit, approval and route. That continuation
  reached approved update/rollback, then stopped on the verifier lookup below.
  It is **not reported as a complete fresh acceptance run**.
- Separate subsequent probes verified the existing EBM fit's exact lineage,
  HTTP approval/native inference, capacity rejection and all seven real Training
  faults. Every fault job had one attempt and stop proof; transport drained to
  0/0; MLP and EBM still returned 200. These supplementary probes do not replace
  the required fresh CI gate.

Observed fixes are confined to the verifier:

1. `kubectl exec` does not inherit the private environment loaded by PID 1.
   Every operator command now executes through the existing strict entrypoint.
2. Execute the packaged fixture as a module so its packaged test-input helpers
   resolve without mounting host source.
3. Gateway shares Control's Pod; restart and verify both ready containers and
   the replacement Pod UID rather than assuming a separate Gateway Deployment.
4. MLflow 3.6/psycopg's GetModelVersion parameter causes an integer/string SQL
   comparison error. Use the existing EBM verifier's supported version-search
   path and select the exact immutable version; run/manifest/dataset equality
   checks remain mandatory.

## Independent regressions and retained resources

Existing SQLite/PostgreSQL contracts, production-image qualification, ingestion
and complete real-service Compose Backend acceptance remain required. The live
Databricks job remains a separate approved external qualification. K8s success
does not substitute for Compose success or vice versa.

The local Acceptance namespace/PVCs, completed jobs, artifacts, two approved
synthetic endpoints, scoped accounts, bounded operators and failed/partial
receipts remain for inspection. Earlier Foundation/Storage/Serving/Training
namespaces and original untracked drafts were not modified. Credentials, raw
rows, private manifests and diagnostic error logs are excluded from public
evidence. The CI runner is disposable; explicit public artifacts retain its
qualification evidence.

Production acceptance remains false. Cross-node CSI/flock, node partitions,
force deletion, CNI enforcement, enterprise identity/MLflow permissions,
production capacity/SLO/retention/restore and release operations require the
final Production Operations work package. See [ADR-021](../adr/021-kubernetes-acceptance.md)
and the [runbook](../deployment/kubernetes-acceptance.md).
