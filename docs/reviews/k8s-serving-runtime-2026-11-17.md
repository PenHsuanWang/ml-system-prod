# Serving Runtime qualification — 2026-11-17

Baseline: merged PR #8 / master `3150c04`.
Feature branch: `feature/k8s-serving-runtime`; Draft PR #9.

## Result

All seven real Minikube checks passed using the actual production image and
existing model/data/governance services. This qualifies the Serving adapter and
routing behavior in the isolated local environment. Production and cross-Pod
Training recovery remain unqualified.

[Sanitized receipt](../evidence/k8s-serving-runtime-2026-11-17/result.json) records
source/image pins, namespace UID, Kubernetes version, actual runtime Pod UID/image,
22 recovery/rollout events, platform deployment history, Foundation auth/CA smoke
and explicit qualification limits. Runtime source revision is published commit
`d6cf9ac59ba5100d44152955e1a9ab0214aa045b`; subsequent changes concern qualification
scripts, CI configuration and documentation. The source under `src/`, `bootstrap/`,
Dockerfile and project dependencies remains identical to that tested image.

## Executed behavior

| Check | Observed result |
| --- | --- |
| Governed MLP serving | Real training, independent evaluation and explicit approval; prediction succeeds through Gateway and stable native Service DNS. |
| Serving Pod loss | Normal Pod deletion yields a new Pod UID; exact SQL deployment/spec/approval/generation/route revision remain unchanged. |
| Native Deployment loss | Foreground deletion and GC are followed by ACTIVE reconstruction of the same approved platform deployment and Service/spec. Three distinct Pod UIDs were observed for its recovery sequence. |
| Control/Gateway replacement | Replacement leaves the independent Serving Pod unchanged and restores Gateway predictions with the same SQL route. |
| Approved model update | New platform generation activates; previous Pod/native resources disappear before its reservation releases. |
| Rollback | Creates a new deployment and generation using the original approval; no old deployment is resurrected as the route. |
| Governed EBM serving | Synthetic engineering-policy EBM model runs through the same native runtime, materialization and Gateway validation contract. |

The SQL history has generations 1–4. Generations 1–3 are RETIRED with resources
released. Generation 4 is ACTIVE and retains its reservation. Recovery kept the
first route at revision/generation 1; explicit update, rollback and EBM rollout
advance the final route to revision/generation 4. Rollback reuses the original
approved binding but obtains a new deployment/spec identity.

The operator generated three real model fixtures while Relay, Supervisor and
Control were paused. It used the existing local Training supervisor within one
Pod, MLflow tracking/registry/artifact APIs, evaluator, Approval service and SQL
Deployment service. No SQL state/route edits, fake ready executor or substituted
model runtime was used. EBM's named engineering-only thresholds apply only to the
synthetic fixture's recipe resolution, not to global product quality policy.

Two harness issues were corrected during execution: empty Python PID 1 roles used
the earlier Foundation shutdown default, and the fixture initially addressed
`ml-control` instead of the actual `ml-gateway` Service. The fixture now requests
graceful SIGINT for its paused roles, confirms normal Pod deletion, and uses the
correct Service. The successful run resumed already-qualified first-MLP fixtures;
failed diagnostics/receipt remain in the private environment. No force deletion,
SQL release shortcut or automatic reapproval was used.

## Validation

- Packaged CPU acceptance image, no source bind mount and read-only root: **115
  focused Serving/governance/Kubernetes/bootstrap cases passed, zero skipped**.
- Full Backend CI on implementation/qualification commit `dad8a454`: **1115 passed,
  zero failures/errors/skips**, no acceptance blockers; real EBM lifecycle passed.
- SQLite and PostgreSQL contracts, Production image bootstrap, Ingestion container
  and Foundation CI passed. Protected live Databricks is intentionally separate.
- Domain/Application typing, dependency boundaries, production bootstrap typing,
  scoped Ruff/format and the unchanged 40-resource Foundation render passed. Native
  opt-in render has 44 resources. No new dependency or SQL migration was added.
- Control's projected API token is absent from Gateway and Serving; its SA cannot
  read Secrets. Serving has no API token/PVC and MLflow reader writes are denied.
- Existing Foundation Pods remain healthy with zero restarts and the earlier
  Storage qualification PVC/completed reader remain intact.

CI references: [ML #60](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44008036334)
and [Foundation #4](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44008036433).
The PR description carries validation of the final head after evidence is added.

## Retained environment and limits

Namespace `ml-k8s-serving-runtime` retains the ACTIVE EBM model, infrastructure,
operator Pod and private fixtures/receipts. Relay/Supervisor are scaled to zero;
Control/Gateway and Serving are running. Operator lifetime is bounded. The older
Foundation and Storage namespaces were not migrated, reinitialized or removed.

This is single-node Minikube. Node partition/force deletion, cross-node/CSI failure,
HA controllers, production capacity/SLO/restore, arbitrary Secret rotation,
registry loss, CNI enforcement/mTLS and formal model quality remain outside this
qualification. Kubernetes object disappearance cannot certify physical stop on a
partitioned node. Do not release SQL reservations manually under uncertainty.
Training Runtime, observability/lifecycle and Production Operations remain later
engineering PRs. See ADR-019 and the Serving runbook for the trusted-controller
boundary and safe failure handling.
