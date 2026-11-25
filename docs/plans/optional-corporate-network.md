# Optional corporate network build plan

Baseline master e1dc4234c32c6586b31aed5a73d977abaa13348f.
Branch build/optional-corporate-network-support, independent of discovery/hybrid.

## Existing inventory and gaps

Dockerfile.ml uses pip in all API/compute/serving targets and APT in acceptance.
Dockerfile.mlflow uses pip. Dockerfile.minio uses APK, Git and Go module/checksum
services, with exact MinIO/MC commits, license copies and OCI labels. No optional
additional trust input exists. Docker daemon registry trust precedes all RUN
instructions and cannot be configured by this application.

## Implementation decision

Use one optional BuildKit secret named corporate_ca, mounted only in network RUN
steps. A small shared Go standard-library launcher validates supplied PEM CA
certificates, combines them with the image's public roots in a private temporary
file and scopes trust environment variables to one child command. It removes the
file before the layer completes and fails the build if cleanup fails. No secret
content or proxy values are printed. Without the secret, execute the original
command with default trust unchanged. Compile the static launcher offline in a
Go tool stage and bind-mount it into Python/Alpine network steps, so no tool or
corporate CA is copied into runtime images. This also makes CA validation available
before Alpine package installation, without insecure bootstrap.

APT HTTPS receives an explicit scoped CaInfo option; pip, Git, Go and APK use their
verified trust variables. Standard predefined Docker proxy build arguments remain
operator supplied and absent from Dockerfile ARG/ENV/history. Do not disable TLS,
signatures or Go sums. No dependency/release version changes. BuildKit/daemon
proxy, CA and registry mirror setup remain outside this repository's runtime.

## File scope and compatibility

Modify only the three Dockerfiles, targeted ignore rules, existing CI validation
and documentation. Add scripts/build_trust and focused tests/CI qualification.
No runtime generic trust facility is introduced. Existing read-only MLflow CA
mounts and verified production entrypoint remain authoritative. Operators mount
an approved combined bundle for existing external runtime clients when required.
All roles stay nonroot/read-only compatible; Kubernetes topology is untouched.

## Validation matrix

| Requirement | Planned evidence |
| --- | --- |
| Optional default behavior | Clean no-secret target builds and helper tests |
| Valid/invalid trust | Synthetic CA, malformed/non-CA tests and actual optional-secret builds |
| Verified dependency fetches | pip/APT/APK/Go/Git steps retain verification and checksum defaults |
| No persistent material | Inspect exported image filesystem/history/layers for unique synthetic CA marker |
| MinIO integrity | Exact binary versions, commit/license/OCI label checks for server/client |
| All targets | Build API/ingestion/training/EBM/production/acceptance/serving/MLflow/MinIO targets |
| CI portability | Existing clean CI builds require no corporate input; extend existing quality scope |
| External limits | Distinguish restricted Cloud build failures from clean remote CI and enterprise qualification |

Keep production_accepted=false. Never change Docker daemon settings or user runtime
resources. Use unique image tags and synthetic optional trust fixtures only.
