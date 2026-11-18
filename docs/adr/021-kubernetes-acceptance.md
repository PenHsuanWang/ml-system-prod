# ADR-021: Governed Kubernetes acceptance and Compose regression

Status: Accepted for implementation in Kubernetes work package 5.

## Decision

Qualify one immutable production source/image in a new disposable
`ml-k8s-acceptance` namespace using both native Serving and Pod Training.
Keep all normal runtime roles running. Submit training, cancel, approve, deploy
and rollback through authenticated Management HTTP; predict through Gateway.
Never invoke a training supervisor in the acceptance operator, patch SQL state,
manufacture physical-stop evidence or release reservations by deleting records.

Provision only synthetic datasets and scoped dev reviewer/viewer/foreign-tenant
accounts as fixtures. The acceptance overlay explicitly selects a named
`engineering-test-only` EBM policy while retaining the strict production entrypoint
and model environment. Its namespace alone is the disposable test boundary. Those settings
must never enter the reusable production base or other qualification profiles.
An engineering approval exercises the existing approval contract in the isolated namespace; it is not
enterprise-data, model-quality or production approval.

Exercise MLP training/evaluation/approval/inference, idempotent admission,
authorization and tenant isolation, real quality rejection, approved update and
rollback, failed-candidate route preservation, ACTIVE runtime reconstruction,
Control/Gateway restart and EBM raw-input/builder/model lineage. Reuse the seven
real Training fault scenarios in this same namespace, including same-fit Control
takeover, cancel, deadline, drain and Redis/manual-ACK gating.

Use an explicitly pinned acceptance-tools image only for the bounded operator.
The operator has no Kubernetes API token; its tools and fixtures are not installed
in production runtime containers. Host-side fault orchestration is fixed to the
disposable Minikube context/namespace. Secret faults must restore the original
Secret in a finally block without logging its contents. Preserve incomplete runs
and refuse receipt overwrites. Never force-delete Pods or automatically delete
namespaces/PVCs.

## Evidence and gates

Record actual source commits, containerd manifest digests, namespace/Pod UIDs,
SQL route/generation/approval/spec identities, training execution/attempt/stop
receipts, evaluation/model lineage and broker/technical-snapshot observations.
Public evidence uses an allowlist and omits credentials, bearer/owner tokens,
connection strings and source rows. Partial or skipped checks cannot produce a
passing Kubernetes acceptance result.

Keep the existing complete real-service Compose Backend acceptance as an
independent required regression. A Kubernetes success does not substitute for
Compose, and a Compose success does not certify Kubernetes. No workload, loss,
feature, dataset/artifact schema, ownership/route fence, approval rule, global
Serving capacity or default local execution change is planned.

Single-node Minikube acceptance leaves cross-node CSI/flock, node partitions,
force deletion, enterprise identities, CNI enforcement, production SLO/capacity,
backup/restore and release operations to work package 6. Reports must retain
`production_accepted=false` and explicit pending production gates.
