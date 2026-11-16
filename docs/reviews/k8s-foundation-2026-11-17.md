# Kubernetes Foundation — Validation Record

Date: 2026-11-17 (Asia/Taipei)

Result: **FOUNDATION_SMOKE_PASSED**. Production qualification remains pending.

## Scope

The runtime image was built from master
`ce81327fd8bf2908dc0d3dec2c0efb67e04b1612`; no `src/`, `bootstrap/`, model,
governance, database schema or Compose changes were made. The feature adds
Kustomize declarations, private-file adaptation, local installation/smoke tools,
focused contract CI and documentation. The existing MLflow image additionally
installs its optional `auth` dependencies; authentication is selected only by the
local K8s declaration's `--app-name basic-auth` argument.

## Executed checks

- `python scripts/check_k8s.py`: 40 public resources rendered and security/topology
  contracts passed, without cluster access.
- `python scripts/check_boundaries.py`: passed.
- Scoped Ruff lint and formatting: passed for the new helper, verifier, init
  adapter, checker and tests.
- Python 3.12 container: `tests/ml/k8s`, `test_deployment_entrypoint.py` and
  `test_deployment_qualification.py`, with `--noconftest`: **110 passed**.
  Foundation tests include migration failure preventing runtime apply, existing
  namespace rejection, digest requirements, role allowlists and private-file
  projection/ownership/mode/no-overwrite behavior.
- Real Minikube v1.39.0, Docker driver, Kubernetes v1.37.0: final declarations
  completed a fresh installation without manual correction.
- Six infrastructure Deployments ready; MinIO initialization and Alembic Job
  complete. Five application Deployments ready, including Control/Gateway's
  two containers. All final-install application containers had zero restarts.
- Seven role init containers passed the unchanged production configuration
  validation; six long-running application roles ready.
- Management and Gateway readiness: passed. This does not prove model readiness.
- Local MLflow: verified test CA accepted, untrusted CA rejected, unauthenticated
  API access rejected, separate reader could read the retained experiment and
  could not rename it. This is a scoped smoke, not formal artifact/enterprise
  authorization qualification.
- Final diff whitespace check: passed. Original untracked workspace drafts
  excluded. Credentials and rendered private manifests remain under ignored `var/`.

See the sanitized [machine-readable result](../evidence/k8s-foundation-2026-11-17/result.json).
Image references are actual containerd manifest/index digests; no Docker config
ID was substituted for an image digest. MinIO server/client images were reused
from the existing local source-built images; the runbook documents rebuilding
them from the unchanged `Dockerfile.minio`.

## Issues found and corrected during development

1. MLflow's default worker count exceeded the initial memory budget. Pin one
   worker and declare a 1536 MiB memory limit for this local profile.
2. Nginx attempted to write default temporary paths on a read-only filesystem.
   Explicitly place all temporary/pid paths under its writable `/tmp` volume.
3. An fsGroup-writable emptyDir root remains root-owned; a non-root init cannot
   chmod that mount root. Create a UID-owned `config/` child and keep role/CA
   copies at `0400`, without relaxing the existing entrypoint checks.

The first development namespace and its test data were cleaned up through normal
deletion. Its configured termination grace and namespace reclamation took time;
no finalizers were removed and no uncertain training was force-declared stopped.
The final fresh namespace is retained for inspection. The helper remains a
fresh-install workflow and refuses existing namespaces; it performs no cleanup.

## CI acceptance-image correction

The first full Backend CI run exposed a packaging omission: `Dockerfile.ml`'s
`acceptance` target copied tests and scripts but omitted `deploy/k8s`. Four
private-file adaptation cases failed with `FileNotFoundError`; the remaining
1,052 tests and the separate EBM end-to-end case passed. The standalone
foundation job had the full checkout and therefore did not expose the omission.

The fix copies the public K8s declarations into the acceptance target only and
requires migration-ordering tests to find their mandatory manifest fixtures.
The rebuilt image, with a read-only root and no source bind mount, passed all
110 Foundation/entrypoint/qualification regressions. Scoped lint, dependency
boundaries and manifest contracts also passed. Foundation cases remain in the
full Backend suite with zero-skip acceptance; production targets, application
behavior and test exclusions are unchanged.

## Remaining gates

Real CSI and cross-node storage contracts, ACTIVE-serving rebuilding and route
recovery, Pod-aware training/confirmed termination, enforcing-CNI network tests,
enterprise identities/MLflow artifact qualification, HA, backup/restore and
capacity/alerting evidence belong to the subsequent work packages. Databricks is
disabled locally and requires live qualification only when used by the deployed
workload. No production approval or full ML lifecycle acceptance is inferred.
