# Hybrid external MLflow implementation plan

Baseline master e1dc4234c32c6586b31aed5a73d977abaa13348f.
Branch feature/hybrid-external-mlflow. Independent of discovery/build changes.

## Investigation and gap

Reference compose.ml.yaml bundles MLflow, its database and MinIO with shared dev
connection variables. compose.ml.external-mlflow.yaml is standalone, with strict
production role files and HTTPS, but has no local infrastructure. Overriding the
reference tracking URI leaves bundled services and dependencies in the graph.
Existing deployment_entrypoint validates separate writer/reader identities and
CA, and LocalServingLauncher forwards only the reader plus approved CA. Preserve
both mechanisms and existing profile semantics.

## Smallest compatible design

Add a standalone hybrid Compose model. Reuse the existing strict production
entrypoint, pinned release/source and per-role files, so no second credential
loader is necessary. The topology is suitable for isolated hybrid development;
prod settings are a security contract, never a production qualification claim.
Add local postgres/mongo/rabbitmq/redis with private, per-infrastructure env/config
files and no host ports. No bundled tracking/artifact services exist in the model.
Management and ingestion are opt-in independently of optional Databricks config.
Keep migration-before-runtime and gateway network_mode service:control.

Extend offline deployment preflight with an explicit hybrid selection that allows
only these four infrastructure services. Default external preflight stays strict.
No reset/override YAML extensions or new Compose minimum are required. The init
wizard's existing compose/external modes remain unchanged; hybrid setup is an
explicit operator procedure. No runtime endpoint switch or registry migration.

## Files and preserved boundaries

New compose.ml.hybrid-mlflow.yaml, public .env example, hybrid tests/qualification
fixture script, this plan and runbook additions. Modify deployment_cli preflight
only for explicit topology choice. Parameterize the existing live lifecycle test's
approval environment for the strict-role fixture. Extend existing CI/gates only
where necessary. Do not change domain, training, outbox, ACK, leases, serving
launcher, registry, dataset or Databricks source contracts.

## Acceptance/evidence matrix

| Requirement | Planned validation |
| --- | --- |
| Local infra + external MLflow only | Real Compose model, dependency/namespace negative tests |
| Role credential/TLS boundaries | Existing entrypoint + preflight regression, writer/reader negative tests |
| External dependency qualification | Existing deployment_cli qualify-mlflow against isolated HTTPS/auth artifact proxy fixture |
| Full governed flow | Real SQL/MQ/worker/registry/evaluation/approval/controller/serving/gateway test |
| Profiles unchanged | Existing initialization six-model render and deployment regression |
| Cutover/rollback | Document fresh state or explicitly qualified migration, recreate runtime |
| Enterprise/K8s | Separate external qualification, no production acceptance claim |

Use unique projects, synthetic accounts, generated CA and fresh runtime directories.
Keep production_accepted=false. Fixture evidence must identify its actual source,
image and runtime adjustments, with no enterprise endpoint or secret disclosure.
