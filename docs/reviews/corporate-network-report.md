# Optional corporate network engineering report

## A. Repository baseline

Baseline master e1dc4234c32c6586b31aed5a73d977abaa13348f.
Branch build/optional-corporate-network-support, work package C.
Final implementation source bf69ef705091d4778e1ee87f7a4b21a5628401d6. Documentation/evidence commits follow.
Independent of both feature branches; no stacking or duplicated implementation.

## B. Requirement traceability

| Requirement | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| 7.1/7.3 optional CA + proxy | Optional BuildKit corporate_ca; standard predefined proxy args | Go default-trust and real optional client builds | INTEGRATION_TESTED |
| Preserve public roots/TLS | Combined scoped bundle, strict CA validation | Synthetic HTTPS rejection/acceptance, pip/APK/APT/Git/Go | INTEGRATION_TESTED |
| Valid/invalid CA and cleanup | Bounded regular-file/PEM/CA/date checks, cleanup before layer success | Seven Go tests; invalid Docker build expected rejection | UNIT_TESTED |
| APT sandbox retained | Private temporary bundle owned by _apt | Real signed HTTPS APT update/install | INTEGRATION_TESTED |
| No published CA/proxy material | Stage bind mounts, no proxy ARG/ENV, no global trust writes | Exported PEM/DER scans, image history and environment | INTEGRATION_TESTED |
| 7.4 runtime trust boundary | Existing read-only MLflow CA mechanism retained | Source inspection and full Python regression | IMPLEMENTED |
| 7.5 MinIO integrity | Exact MinIO/MC commits, licenses, OCI labels, checksum defaults | Static contracts, actual fixed module/tag lookup, CI builds | INTEGRATION_TESTED |
| 7.6 context safety | Targeted .local/corporate-network exclusion in both ignores | Isolated Git rule tests and runtime image scans | UNIT_TESTED |
| All runtime/test targets | Existing default CI plus explicit serving/k8s-acceptance builds | Deployment/ingestion/Backend jobs | COMPOSE_QUALIFIED (engineering CI) |
| Standard clean builds | No secret required; default trust preserved | Clean GitHub CI, separate from intercepted Cloud | COMPOSE_QUALIFIED (engineering CI) |
| Existing semantics | No domain/runtime/dependency-version changes | 1326 passed, 5 existing opt-in skips | UNIT_TESTED |
| Kubernetes operations | Build target only; no topology/runtime change | No new live cluster qualification | DEFERRED |

## C. Changed files

A means added, M modified; no deleted files. Full inventory is generated below.

| File | Type | Purpose | Architecture impact |
| --- | --- | --- | --- |
| .dockerignore | M | Targeted private input exclusion | Build/test only; runtime contracts unchanged |
| .github/workflows/ml-ci.yml | M | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| .gitignore | M | Targeted private input exclusion | Build/test only; runtime contracts unchanged |
| Dockerfile.minio | M | Optional verified build-only trust | Build/test only; runtime contracts unchanged |
| Dockerfile.ml | M | Optional verified build-only trust | Build/test only; runtime contracts unchanged |
| Dockerfile.mlflow | M | Optional verified build-only trust | Build/test only; runtime contracts unchanged |
| FILE-MANIFEST.json | M | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| README.md | M | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| docs/configuration-reference.md | M | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| docs/deployment/day1-deployment-runbook.md | M | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/apt-https.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/go-final-head.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/go-git.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/go-unit.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/invalid-ca.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/no-leak.json | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/optional-api.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/optional-apk.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/qualification.json | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/regression-delivery.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/regression-disk-failure.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/regression.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/evidence/corporate-network-20261126/standard-api.txt | A | Validation evidence/source inventory | Build/test only; runtime contracts unchanged |
| docs/implementation-status.md | M | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| docs/plans/optional-corporate-network.md | A | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| docs/reviews/corporate-network-report.md | A | CI, operator or review documentation | Build/test only; runtime contracts unchanged |
| scripts/build_trust/main.go | A | Optional verified build-only trust | Build/test only; runtime contracts unchanged |
| scripts/build_trust/main_test.go | A | Trust safety and image contract validation | Build/test only; runtime contracts unchanged |
| tests/ml/build_network/fixtures/Dockerfile | A | Trust safety and image contract validation | Build/test only; runtime contracts unchanged |
| tests/ml/build_network/test_build_contract.py | A | Trust safety and image contract validation | Build/test only; runtime contracts unchanged |

## D. Architecture impact

A single small Go standard-library launcher is compiled offline in a build-only
stage, then bind-mounted into network RUN steps. This supplies CA validation
before APK/Python dependencies are available. Optional additional roots augment
the base public roots in a private temporary bundle. Only the child package tool
receives scoped trust settings. APT receives CaInfo and retains its unprivileged
fetch identity; no sandbox or TLS bypass is used. Cleanup failure aborts the layer.
No launcher binary or build-only CA is copied into runtime targets.

All twelve application/test/MLflow/MinIO build targets retain their existing
runtime contract. Acceptance images additionally carry the public Dockerfiles
and ignore rules needed by new build contract tests. There is no new runtime
service, trust loader, schema, permission, MLflow endpoint switch or model change.

## E. Security review

CA input is optional, bounded to 1 MiB and validated as current CA certificates.
Non-CA/private-key/malformed/nested PEM, invalid signing usage and expired inputs
are rejected. CA certificates without an optional KeyUsage extension remain valid.
Base public roots, certificate verification, APT signatures and Go sums remain.
Proxy build args use Docker's predefined handling and are not declared/persisted
in Dockerfiles. Input folders are excluded from Git and build context. PEM and DER
canary scans found no added certificate in exported API/Alpine image layers, with
no scoped trust variables in runtime environments. Runtime identities and existing
read-only CA mounts are unchanged. No host or daemon trust store was modified.

## F. Validation results

[Machine-readable evidence](../evidence/corporate-network-20261126/qualification.json)
records exact commands, statuses, counts, exit codes, source notes and log hashes.
Seven final Go tests and six Python build contract tests pass. Full local ML
regression passed 1326/0/5. A prior parallel run exhausted Cloud disk and produced
1316 passes, 1 failure and 9 setup errors; the isolated rerun passed. This failure
is retained rather than reclassified as a successful execution.

Real optional CA builds passed for API/pip and Alpine/APK. Dedicated retained
fixtures passed APT HTTPS, signed package metadata, Git HTTPS against the unchanged
MinIO release tag, and Go module proxy/checksum resolution at its exact commit.
An invalid CA fails a no-cache build before package access. Two complete exported
image layer/history/environment scans pass. Initial CA validation was corrected
to accept standards-compatible absent KeyUsage while rejecting malformed nested
PEM. Final Go tests cover that parser tightening.

The Cloud standard no-secret API build failed verified TLS because this execution
network intercepts TLS; its status is BLOCKED, not PASS. Default clean builds use
GitHub-hosted public trust without any corporate input. Earlier CI caught missing
public Dockerfiles in acceptance image test inventory; those files are now copied
into engineering images and all assertions retained. New CI checks build the
separate serving and Kubernetes acceptance targets and exercise valid/invalid
synthetic trust. Kubernetes image build does not qualify cluster operation.

## G. Remaining risks and blockers

Actual enterprise proxy/mirror/registry policy remains EXTERNAL_ENVIRONMENT_NOT_VERIFIED.
Dockerfile RUN steps cannot configure pre-FROM daemon or registry trust. Operators
must approve additional CAs and proxy configuration and protect private inputs.
BuildKit secret changes do not invalidate cache; qualification/rotation requires
appropriate clean builds. Bases must contain public roots; APT bases need their
standard _apt identity. No enterprise CA is baked into generic runtime images.
Dynamic Kubernetes qualification is deferred. production_accepted remains false.

## H. Merge recommendation

READY FOR REVIEW for the engineering change set, subject to the observed final
CI state linked in the retained evidence. This is not production approval.
Proposed PR title: build: support optional corporate proxy and CA configuration.
This report and the linked operator runbook supply the proposed PR description.

The complete final-code [CI run 45682995410](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/45682995410)
on bf69ef7 passed. Observed steps include standard production, serving and K8s
acceptance image builds; valid/invalid synthetic CA and no-leak checks; real
private nonroot entrypoint verification; ingestion container; and complete
Backend/Compose acceptance. All supported build targets are covered through these
existing job dependencies. The retained hosted job-step record identifies actual
completed validation, without inventing unavailable JUnit counts.

Local K8s static rendering passed 40 foundation resources after putting the already
verified kubectl binary on this command's PATH. The initial missing-PATH failure is
retained. The K8s acceptance lint/contract gate passed. No cluster was accessed.
Source code recommendation is READY FOR REVIEW; production_accepted remains false.
