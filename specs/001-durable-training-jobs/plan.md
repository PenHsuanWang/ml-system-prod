# Durable Training Jobs implementation plan

Status: implementation baseline following the user's request to start coding. This does not change the Spec's production approval status. Product retention/SLO decisions remain release gates. Decisions are recorded in [ADR-014](../../docs/adr/014-implementation-baseline.md).

| Contract | Implementation | Verification |
| --- | --- | --- |
| Atomic accept | TrainingService, SQL UoW, tenant admission lock, separate request/spec digests | parallel acceptance, expiry reuse, replay/backend drift |
| Durable delivery | transactional Outbox, leases, mandatory returns and publisher confirms | lease fencing; opt-in real Taskiq Receiver test |
| Single owner | Job-before-Attempt locking, owner-token guards and partial unique live-attempt index | competing consumers and stale writes |
| Real cancellation | durable intent, atomic pending close, process identity/group stop proof | physical termination, PID reuse, unknown identity |
| Recovery | prelaunch record, boot/start ticks, nonterminal uncertainty, five automatic recovery claims | no false stop proof, bounded recovery |
| Terminal truth | durable manifest, canonical model binding and matching evaluation | completion/cancel race, evidence mismatch |
| HTTP isolation | JWT/current Mongo sessions, scoped reads, safe DTOs | ASGI authorization/validation tests |
| CPU path | supervised NN, train-only transforms, checkpoint/resume, exact bundle evaluation | real subprocess, resume and inference tests |

Freeze the bundle before Registry allocation. Store the assigned version externally, evaluate the unchanged bundle, then reference the exact EvaluationResult in Approval and the exact Approval in Deployment. Routing uses an independent revision to fence delayed activation before cleanup.

Concrete source is implemented; real PostgreSQL/RabbitMQ/MLflow and role-image qualification remain environment evidence. The original approval checklist has not been automatically checked.
