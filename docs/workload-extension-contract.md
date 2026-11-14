# Workload Extension Contract v1

Training dispatch uses two trusted, static dictionary-based mappings. Neither is a
runtime plugin API; both are exposed as read-only mappings after validation.

| Exact approved Recipe | Workload ID | Training handler |
| --- | --- | --- |
| `nn_classifier@1.0.0` | `nn_classifier` | `_train_mlp` |
| `ebm_poc_replay_v1@1.0.0` | `ebm_poc_replay_v1` | `_train_ebm` |

`src/domain/training/workloads.py` defines the immutable `WorkloadContract` values
and explicitly constructs `APPROVED_WORKLOADS`. `workload_for_spec()` remains the
supported resolver. Recipe objects must contain exactly `id` and `version`, and
resolution matches that pair without aliases or version fallback. Duplicate Recipe
registrations, invalid definitions, and conflicting metadata for the same Workload
ID fail during construction. Distinct Recipe versions may share the same identical
contract; incompatible contracts need distinct Workload IDs.

Compatibility remains unchanged: legacy MLP specs may omit workload metadata and
receive the existing MLP defaults. Explicitly supplied metadata must match. EBM
specs require all existing workload/component fields. Workload IDs, evaluator
versions, runtime compatibility IDs, input/output schemas and artifact profiles
retain their existing meanings and values.

`bootstrap/ml_training_runner.py` explicitly constructs `TRAINING_HANDLERS`, keyed
by approved Workload ID. Its callable contract receives the resolved spec, output
directory, progress callback, producer provenance and platform spec digest.
`run_training()` resolves and validates the Workload Contract, looks up its Handler,
and invokes it. Missing handlers raise `MLError`; there is no default Trainer.
Duplicate, unapproved or non-callable handler registrations also fail closed.

Each handler preserves the existing architecture/resource-profile checks, approved
dataset loading, model/training configuration, progress reporting and Trainer call.
Heavy dependencies remain lazily imported within handlers. Trainer checkpoint and
resume code, output bundles and artifact formats are unchanged. This change adds
no new resume behavior to the child entry point.

To add a workload in a future reviewed PR:

1. Define its immutable contract and exact approved Recipe pair in the Domain mapping.
2. Implement a trusted bootstrap handler and explicitly register its Workload ID.
3. Add compatibility, rejection, dispatch and bounded real-training tests, together
   with any separately reviewed admission/resource/dataset contracts it needs.

Registrations never come from requests, environment variables, import paths, class
names or user-supplied callables. There is no dynamic discovery, third workload,
new dependency or persistent registry in v1. Recipe admission, public request
schemas, Supervisor/Worker/Outbox behavior, Evaluation, Serving and Deployment
extension remain separate concerns; registration alone does not enable them.

Focused verification: `python -m pytest tests/ml/test_workload_dispatch.py -q`.
Existing MLP/EBM child, training/resume and platform regressions remain applicable.
