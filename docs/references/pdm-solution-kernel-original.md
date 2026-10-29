# PdM & Solution Kernel
## Software Architecture Design Specification

**Document ID:** PDM-SK-SAD-001  
**Architecture baseline:** Runtime-Oriented Relationship Architecture  
**Version:** 1.0  
**Date:** 23 October 2026  
**Status:** Release Review — Approval Pending  
**Classification:** Internal Engineering Design  
**Primary audience:** Software Engineering, Data Science, Solution Engineering, Platform/IT, and Technical Reviewers  

> **Release integrity statement.** This specification records the agreed target architecture and its normative implementation boundaries. It does not assert that individual components, controls, integrations, or operational service levels have already been implemented, tested, or approved. Release approval must be supported by the evidence defined in Section 12.

## Document Control

| Field | Value |
|---|---|
| Document owner | Software Architecture / Solution Engineering (to be assigned) |
| Technical approver | To be assigned |
| Review stakeholders | Backend Engineering, Data Science, Platform/IT, Security, Operations |
| Architecture diagram | `pdm_solution_kernel_runtime_relationship_final.svg` |
| Source of truth | This specification together with the version-controlled diagram and decision records |
| Revision | 1.0 — initial release-review baseline |

## 1. Executive Summary

PdM & Solution Kernel is a modular Python backend architecture intended to support diagnostic workflows across multiple fab, tool, and algorithm configurations. It separates **runtime execution**, **inbound transport**, **platform runtime capabilities**, **application workflow orchestration**, **domain contracts**, **pluggable algorithms**, and **external-system adapters**.

Two independent entrypoints serve different execution profiles: `server.py` hosts the FastAPI/ASGI interface, while `worker.py` runs background or compute-intensive jobs. Both enter the same application use cases without requiring worker-to-API HTTP calls. A dedicated `bootstrap/` composition root initializes process-local resources, loads approved plugins, and wires concrete implementations into use cases; bootstrap is **not** part of the normal request-processing chain.

The architecture applies an inward source-code dependency rule: the application, algorithm plugins, and infrastructure adapters depend on stable domain contracts; the domain package must not import outer-layer modules. `services/` is deliberately defined as **Platform Runtime Services**, not a business-service layer. Its responsibility is technical runtime reliability, whereas `application/` owns business use cases and orchestration.

**Expected architectural outcomes:** independently scalable API and worker execution, clearer ownership between software and data science teams, replaceable infrastructure adapters, testable use cases, and controlled extension of Fab/Tool solutions. These are design objectives; operational effectiveness must be demonstrated through implementation and release validation.

## 2. Purpose, Scope, and Design Drivers

### 2.1 Purpose

This document establishes the package boundaries, dependency rules, initialization model, runtime interactions, extension contracts, and verification criteria for the PdM & Solution Kernel Python backend. It is intended to guide implementation, code review, integration, and future architecture changes.

### 2.2 In Scope

- Python entrypoints for the HTTP control path and background-compute path.
- FastAPI inbound request handling and transport-to-use-case translation.
- Platform runtime lifecycle, resource management, health, observability, and concurrency safeguards.
- Application orchestration of diagnostic use cases and Global → Fab → Tool configuration resolution.
- Stable domain entities, diagnostic strategy interfaces, and outbound ports.
- Algorithm plugins and external-system adapters with defined ownership boundaries.
- Composition-root dependency wiring, configuration management, import policy, and release validation requirements.

### 2.3 Out of Scope / Not Implied by This View

- A specific message broker, task framework, workflow engine, container orchestrator, or cloud deployment topology.
- Detailed SQL schemas, model feature contracts, model-training pipelines, and individual algorithm internals.
- Production SLO values, capacity limits, retry budgets, and retention policies that have not yet been formally approved.
- An assertion that a transactional outbox, distributed lock, job-status API, or any particular integration is already operational.

These areas require separate interface specifications, deployment diagrams, or architecture decision records (ADRs).

### 2.4 Primary Quality Drivers

| Driver | Architectural response |
|---|---|
| Maintainability | Small, named packages with explicit, testable dependency rules. |
| Extensibility | Domain-defined strategy contracts; configured plugin selection rather than use-case imports of concrete algorithms. |
| Runtime stability | Independent API/worker processes with lifecycle-managed resources and bounded concurrency. |
| Testability | Use cases depend on injected abstractions; adapters and plugins can be replaced by test doubles. |
| Portability | Infrastructure integrations are isolated behind domain ports; deployment technology is not embedded in business logic. |
| Cross-team ownership | Data/Domain Scientists own solution plugins; Software Engineers own shared runtime and adapters. |

## 3. Architectural Principles and Normative Language

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** specify mandatory constraints, prohibited designs, strong recommendations, and permitted options, respectively.

1. **Single responsibility by layer.** Transport logic MUST remain in `api/`; business workflow orchestration MUST remain in `application/`; runtime operations MUST remain in `services/`.
2. **Inward dependencies.** `domain/` MUST NOT import `api/`, `services/`, `application/`, `plugins/`, `infrastructure/`, or `bootstrap/`.
3. **Implementation independence.** `application/` MUST NOT statically import specific plugin classes or concrete infrastructure adapters. It MAY import stable domain entities and interfaces.
4. **Composition at the edge.** Construction and binding of concrete dependencies MUST occur at the composition root or designated provider factories, not inside domain logic or use-case methods.
5. **Worker reusability.** A worker MUST be able to invoke application use cases without making an internal HTTP round-trip through the API layer.
6. **Operational separation.** Runtime support MUST NOT determine Fab/Tool business decisions or perform model-specific evaluation.
7. **Explicit extension contracts.** A plugin MUST satisfy the applicable domain interface and documented input/output, failure, and versioning expectations.
8. **Process awareness.** Resource ownership, pools, model instances, and shutdown behavior MUST be defined per execution process rather than assumed globally shared.

## 4. Logical Architecture and How to Read the Diagram

**Figure 1. PdM & Solution Kernel — Runtime-Oriented Relationship Architecture** (see full-size SVG supplied with this specification).

The diagram is read from the **upper-left runtime entrypoints** toward the **application and domain contracts**, with independently reusable support packages grouped on the far left. The location of a box does not, by itself, establish either a call sequence or an import dependency. Relationship semantics are defined by the line type and label.

| Relationship | Meaning | Representative edge |
|---|---|---|
| Runtime invocation | A component invokes another during request/job processing. | `server.py → api/`; `api/ → application/`; `worker.py → application/` |
| Bootstrap wiring | Initialization constructs, loads, registers, or injects components. | `bootstrap/ ⇢ application/`, `plugins/`, `infrastructure/`, `services/` |
| Static dependency | Source code imports stable types, abstract interfaces, or protocols. | `application/ → domain/` |
| Strategy implementation | A concrete plugin implements a domain strategy contract. | `plugins/ → domain/strategy.py` |
| Port implementation | A concrete adapter provides a domain-defined external capability. | `infrastructure/ → domain/ports.py` |
| Optional runtime support | API or worker obtains cross-cutting technical capability, not a required business-layer hop. | `api/ ⇢ services/` |

**Critical interpretation:** a call from a use case to an injected plugin instance is a runtime invocation; the plugin's source-code dependency still points inward to the domain strategy interface. These two directions are compatible and must not be conflated.

## 5. Package and Ownership Specification

### 5.1 Entrypoints — `server.py`, `worker.py`

`server.py` is responsible for constructing/starting the ASGI application, registering the inbound routing surface, and coordinating application lifespan. It MUST NOT contain diagnostic algorithms, feature engineering, or Fab-specific decision rules.

`worker.py` is responsible for starting the background execution environment and invoking the same application use cases through an appropriate task adapter. It MUST NOT require internal HTTP calls to `server.py` or `api/`. The concrete scheduling, broker, and acknowledgement mechanism remains an integration decision.

### 5.2 Composition Root — `bootstrap/`

| Module | Intended responsibility |
|---|---|
| `config.py` | Resolve runtime configuration and required secrets through approved configuration providers. |
| `container.py` | Construct resource owners, runtime services, outbound adapters, and other required objects. |
| `plugin_loader.py` | Load, verify, and register approved plugin implementations; enforce package and contract compatibility. |
| `wiring.py` | Bind implementations to domain interfaces and construct use-case/provider graphs. |

A **wire** is an assignment of a constructed implementation to a consumer's declared dependency, for example `ExecuteDiagnosticUseCase(repository=DatabricksRepository(...), strategy=ResolvedStrategy(...))`. Wiring is an initialization/composition relationship, not a mandatory request-time function call. Lazy or request-scoped providers MAY build objects on demand, but their binding policy remains defined by the composition root.

### 5.3 Inbound Control — `src/api/`

`routers.py` owns HTTP routes and status-code semantics. `schemas.py` owns transport request and response schemas. `dependencies.py` resolves use-case instances/providers. `auth.py` integrates identity and authorization enforcement. `exceptions.py` maps known application failures to appropriate HTTP responses.

The API layer MUST validate and adapt inbound requests and then delegate a typed command/query to `application/`. It MUST NOT embed algorithm selection rules, query external repositories directly in route handlers, or implement diagnostic workflows.

### 5.4 Platform Runtime Services — `src/services/`

| Module | Responsibility | Explicit boundary |
|---|---|---|
| `lifecycle.py` | Startup and graceful shutdown coordination. | Does not own business execution order. |
| `resource_manager.py` | Manage the lifetime of DB, Redis, and external client pools. | Does not implement domain queries. |
| `health.py` | Liveness, readiness, and configured dependency checks. | Does not determine diagnostic status. |
| `observability.py` | Structured logging, metrics, tracing, correlation context. | Does not decide domain outcomes. |
| `concurrency.py` | Technical limits, admission control, process-local execution safeguards. | Does not replace distributed business locks. |

`services/` and `application/` are peer capabilities with distinct responsibilities. `services/` MUST NOT be a mandatory intermediate hop for every use case. Concrete clients and their domain-facing operations belong in `infrastructure/` or an explicitly defined technical adapter; `services/` may manage their lifecycle without assuming their business responsibilities.

### 5.5 Application Orchestration — `src/application/`

`use_cases.py` exposes meaningful application operations. `registry.py` resolves an already-registered strategy based on requested identity and configuration. `hierarchy.py` resolves the Global → Fab → Tool configuration hierarchy. `orchestration.py` coordinates the diagnostic sequence **Lock → Fetch → Evaluate → Publish**, with well-defined error paths and cleanup.

The application is allowed to express execution policy, sequencing, transaction boundaries, and orchestration decisions. Domain-specific invariants and algorithm rules MUST remain in the domain or concrete plugin as appropriate. The application MUST receive repositories, locks, publishers, and strategies through injected interfaces; it MUST NOT instantiate specific vendor adapters or algorithm implementations inside a use case.

### 5.6 Domain Core — `src/domain/`

`models.py` defines stable domain data types such as `AssetIdentity`, `EvaluationContext`, and `DiagnosticReport`. `strategy.py` defines contracts such as `DiagnosticStrategy`, `PipelineInterceptor`, and capability interfaces such as `ArtifactMountable`. `ports.py` defines outbound capabilities such as `DataRepositoryPort`, `DistributedLockPort`, and `EgressPublisherPort`.

The domain core MUST NOT import runtime frameworks, database drivers, FastAPI, concrete plugins, or infrastructure adapters. Neutral third-party type dependencies, if any, require explicit approval and compatibility review; “pure core” means **no outer-layer dependencies**, not necessarily zero imports outside the standard library.

### 5.7 Algorithm Plugins — `src/plugins/`

`inference/` contains ML model diagnostic implementations; `heuristics/` contains rule-based solutions; `interceptors/` contains Fab/Tool-specific pre/post processing behavior. Data/Domain Scientists primarily own algorithm implementation, while changes to shared domain contracts require architecture coordination. Plugins implement domain-defined strategy or interceptor contracts and MUST NOT import the API or application orchestration layer.

### 5.8 Driven Adapters — `src/infrastructure/`

`persistence/` implements retrieval and persistence capabilities; `messaging/` implements broker/publisher integrations where selected; `locking/` provides distributed lock adapters; `downstream/` integrates result consumers and external APIs. Software Engineering owns adapter reliability, configuration, and integration contracts. All such implementations SHOULD be replaceable behind stable domain ports and MUST NOT introduce reverse imports into the domain.

### 5.9 Independent Supporting Packages

`src/config/` contains environment settings and technical constants; `src/utils/` contains small reusable utilities; `src/shared_models/` contains neutral DTOs and common structures. These are **optional shared dependencies**, not an ordered execution chain. Business-bearing entities remain under `domain/`; shared packages MUST NOT become an uncontrolled dependency bypass around architecture boundaries.

## 6. Dependency and Import Policy

The following matrix defines the package-level policy. “Allowed” is intentionally narrow; using a type from a permitted package does not permit invoking unrelated responsibilities in that package.

| Importing package | Allowed internal dependencies | Prohibited or restricted dependencies |
|---|---|---|
| `domain/` | Domain submodules and approved neutral types only | `api/`, `services/`, `application/`, `plugins/`, `infrastructure/`, `bootstrap/` |
| `application/` | `domain/` plus approved neutral support packages | Direct imports from `api/`, `services/`, concrete `plugins/`, concrete `infrastructure/`, FastAPI |
| `api/` | Public application entrypoints, transport DTOs, approved runtime-service interfaces | Direct domain algorithm execution; concrete plugins/adapters |
| `services/` | Technical utilities, configuration interfaces, approved technical resource abstractions | Fab-specific rules, application workflow policies, concrete diagnostic algorithms |
| `plugins/` | Domain contracts, approved scientific libraries, neutral utilities | `api/`, `application/`, FastAPI runtime services, concrete infrastructure adapters |
| `infrastructure/` | Domain outbound ports, integration SDKs, approved neutral utilities | `api/`, application orchestration, plugin selection logic |
| `bootstrap/` and entrypoints | Packages required for assembly/startup | Embedded business or model algorithms |

**Enforcement.** CI MUST execute automated import-boundary checks, unit tests, plugin contract tests, and static analysis. Dynamic importing in `plugin_loader.py` MUST use a controlled registry/allowlist; static import-linting alone does not validate dynamic module names or runtime contract adherence.

## 7. Initialization, Dependency Injection, and Lifecycle

### 7.1 Startup Sequence

1. An entrypoint starts a dedicated Python process (ASGI server or worker runtime).
2. `bootstrap/config.py` resolves process configuration and required secrets; invalid required configuration fails startup explicitly.
3. `bootstrap/container.py` creates process-owned resource managers and selected infrastructure clients/adapters.
4. `bootstrap/plugin_loader.py` discovers and validates configured approved plugins; unsupported or incompatible plugins fail according to the documented startup policy.
5. `bootstrap/wiring.py` injects ports, strategy providers, and runtime services into application providers/use cases.
6. The process completes readiness checks and begins accepting its respective requests or jobs.

### 7.2 Scope and Ownership

| Scope | Examples | Requirement |
|---|---|---|
| Process | Connection pools, initialized logging exporters, model cache | MUST NOT be assumed shared with sibling processes. |
| Request | Request context, request-specific authorization, transaction/session | MUST be disposed or returned after the request. |
| Job | Cancellation context, job-scoped lease, temporary artifacts | MUST be cleaned up on success, failure, or cancellation. |
| Shared external system | Database, Redis, broker, artifact store | Access MUST be mediated through configured clients and contracts. |

### 7.3 Shutdown Sequence

The runtime SHOULD stop accepting new work, handle in-flight tasks per policy, release job locks/leases safely, flush telemetry, close connections and clients, and terminate cleanly. Timeouts and termination behavior are deployment decisions that MUST be defined before production approval.

### 7.4 Wiring Versus Importing

**Importing** is a Python source-code dependency on a module or symbol. **Wiring** is runtime construction and injection of an implementation object. **Invoking** is executing an operation on an available object. Example: the application imports `DiagnosticStrategy`, bootstrap constructs `XGBoostDiagnosticStrategy`, and the application invokes the injected object's `evaluate(context)` method.

## 8. Runtime Interaction Models

### 8.1 Synchronous API Use Case

1. `server.py` receives an HTTP request through the ASGI stack and `api/routers.py`.
2. API validates a transport payload and enforces applicable authorization.
3. `api/dependencies.py` obtains a configured application-use-case instance/provider.
4. API delegates a typed command/query to `application/`.
5. Application resolves Fab/Tool configuration and an injected strategy; it invokes domain-defined ports and strategy contracts.
6. Adapters/plugins execute the concrete operations; Application returns an outcome.
7. API translates the result or known failure into the transport response.

The synchronous path SHOULD be reserved for bounded operations that can complete within the approved request-time budget. Asynchronous Python I/O does not by itself make CPU-bound Python execution parallel.

### 8.2 Background/Compute Use Case

1. An authorized producer or scheduler submits a job through an approved dispatch interface (broker selection is separate).
2. The execution infrastructure delivers a job to `worker.py`.
3. The worker establishes job-scoped context, resolves a use case and delegates execution directly to `application/`.
4. Application coordinates the same domain ports and strategies used by the API path.
5. Job status, result persistence, retry, cancellation, and acknowledgement follow the separately approved job contract.

**Important design boundary:** the architecture drawing intentionally omits the physical broker and task-state components. Their absence from the diagram MUST NOT be interpreted as a brokerless distributed-execution design or a claim of exactly-once processing.

### 8.3 Diagnostic Workflow Semantics

`Lock → Fetch → Evaluate → Publish` is the intended orchestration order. Before release, the workflow specification MUST resolve lock ownership and expiry, telemetry completeness and time alignment, interceptor ordering, algorithm exception mapping, report persistence and publication semantics, and cleanup on partial failure. The architecture does not guarantee those behaviors solely through package separation.

## 9. Domain Contracts and Extension Model

### 9.1 Core Data Contracts

| Contract | Responsibility | Compatibility expectations |
|---|---|---|
| `AssetIdentity` | Fab/area/tool/chamber identifiers and relevant identity boundaries | Canonical identifiers, validation, and explicit optionality. |
| `EvaluationContext` | Data and configuration made available for diagnostic evaluation | Defined units, timestamps, required fields, and versioned features as applicable. |
| `DiagnosticReport` | Standardized diagnostic outcome and trace metadata | Stable schema; clear success, failure, and no-result semantics. |

### 9.2 Strategy and Port Contracts

A `DiagnosticStrategy` exposes an evaluation behavior that accepts a validated context and produces a conforming diagnostic outcome. A `PipelineInterceptor` defines explicitly ordered transformation hooks and MUST document whether it mutates or returns immutable context. An `ArtifactMountable` capability MUST define resource and version lifecycle if model weights are loaded. Ports for repository, locking, and egress MUST document errors, timeouts, retryability, and idempotency expectations.

### 9.3 Plugin Registration and Selection

The **plugin loader** verifies and registers trusted implementations. The **application registry** selects an appropriate registered strategy according to configured Fab/Tool identity; the two responsibilities MUST remain distinct. Selection precedence is `Global → Fab → Tool` with higher-specificity overrides, subject to an explicit merge and conflict policy. Registration MUST detect duplicate identifiers and incompatible contract versions.

### 9.4 Extending the Platform

A new algorithm SHOULD be added as a plugin implementing existing domain strategy contracts, with manifest/registration metadata, configuration examples, model artifact requirements, and contract tests. A new data provider SHOULD be added as an infrastructure adapter implementing the repository port, without changing the business use case. A change to the domain interface is a cross-team contract change and requires versioning and impact review.

## 10. Cross-Cutting Engineering Requirements

### 10.1 Configuration and Secrets

Configuration MUST distinguish environment-specific runtime settings from Fab/Tool business configuration. Secrets MUST be sourced from an approved secure provider, never hardcoded in code, logs, or plugin manifests. Invalid configuration SHOULD fail deterministically and produce actionable diagnostic errors without exposing sensitive values.

### 10.2 Security and Trust Boundaries

Inbound operations MUST apply the organization's authentication and authorization controls. Requests, tool identities, plugin registrations, and outbound destinations MUST be validated. Plugin loading MUST be restricted to approved, versioned code: Python imports execute code and do not create a sandbox. Data and model artifacts MUST follow applicable access controls and audit requirements.

### 10.3 Concurrency and Compute Isolation

API processes SHOULD prioritize responsive I/O-bound request handling. CPU-bound Python tasks SHOULD use suitable background/multiprocess execution rather than blocking request event loops. Worker sizing MUST consider Python process count, native BLAS/PyTorch thread pools, memory per model instance, and CPU/GPU allocation to avoid oversubscription. Process-local semaphores do not replace distributed coordination for shared tool resources.

### 10.4 Reliability and Failure Handling

The implementation MUST define timeout ownership at API, use-case, adapter, and job boundaries; classify retryable versus permanent failures; ensure resource cleanup; and prevent duplicate side effects where jobs may be retried. If a transactional outbox is adopted, business state and pending events MUST be persisted atomically in the same transaction with a separate relay. The word “outbox” alone does not establish this guarantee.

### 10.5 Observability and Audit

The API, worker, application, plugins, and adapters SHOULD share correlation identifiers and consistent structured logs. The audit/trace contract SHOULD capture request or job ID, Fab/Tool identity where permitted, configuration version, plugin/model version, data acquisition provenance, execution outcome, duration, and relevant failure classification. Sensitive data MUST be excluded or appropriately protected.

### 10.6 Health and Operational Readiness

Liveness indicates a responsive process; readiness indicates the process can accept its assigned class of work. Health behavior MUST be specified independently for API and worker processes. Checks SHOULD avoid expensive synchronous operations and distinguish transient external dependency failures from process-level failure.

## 11. Ownership and Change-Control Boundaries

| Area | Primary accountable role | Review required for |
|---|---|---|
| Entrypoints, bootstrap, services, API | Software Engineering | Runtime lifecycle, performance, compatibility, security changes |
| Application workflows and integration contracts | Software Engineering / Solution Engineering | Workflow order, configuration policy, public use-case changes |
| Domain entities, strategies, ports | Joint Software Engineering + Domain/Data Science | Any contract-breaking or semantic change |
| Algorithm implementations | Data / Domain Science | Model behavior, artifacts, interface compatibility |
| Infrastructure adapters | Software Engineering + platform owners | Credentials, connectivity, reliability and external contracts |
| Operational deployment and SLOs | Platform / Operations with owning team | Production capacity, alarms, runbooks, recovery policy |

Ownership describes intended engineering responsibilities, not an assertion about current staffing or existing approvals.

## 12. Verification, Acceptance, and Release Gate

The release candidate MUST be evaluated against the following evidence-based gate. Passing package-layout review alone is insufficient for production approval.

| Gate | Required evidence | Acceptance principle |
|---|---|---|
| Dependency boundaries | Automated import-boundary tests in CI | No forbidden outer-layer imports or unintended cycles. |
| Bootstrap and lifecycle | Startup/shutdown tests per process type | Resources construct, become ready, and close predictably. |
| API / application separation | Endpoint tests with mocked use cases | API contains transport concerns only. |
| Worker parity | Worker tests invoking application directly | No dependency on API HTTP calls. |
| Plugin compatibility | Strategy/interface contract suite | Approved plugins load, validate and satisfy output contracts. |
| Adapter portability | Port contract tests with at least a fake adapter | Business use cases function without a specific vendor SDK. |
| Failure reliability | Timeout, retry, cancellation, and cleanup tests | No leaked locks/resources; clear error classification. |
| Security | Authz and plugin allowlist verification | Unauthorized requests/plug-ins rejected. |
| Observability | Trace/log/metric inspection in staging | Correlated diagnostics across API, worker, and adapters. |
| Load and capacity | Documented benchmark with defined environment | Meets separately approved latency/throughput/resource budgets. |
| Deployment readiness | Runbook, configuration matrix, rollback procedure | Repeatable deployment with reviewed recovery steps. |

### 12.1 Release Approval Record

- Architecture review: **Pending**
- Security review: **Pending**
- Implementation conformance: **Pending**
- Staging verification: **Pending**
- Production release authorization: **Pending**

These fields MUST be completed by designated approvers; approval is not implied by publication of this design specification.

## 13. Open Decisions and Required Follow-Up Specifications

| ID | Decision required | Why it matters |
|---|---|---|
| OD-01 | Background broker/task framework, delivery, acknowledgement and job-state contract | Determines retry, recovery, and job-lifecycle guarantees. |
| OD-02 | Plugin registration format, artifact loading policy, version resolution, and failure behavior | Determines safe extensibility and startup reliability. |
| OD-03 | Distributed lock algorithm, lease duration, renewal, fencing and duplicate execution policy | Prevents conflicting diagnostic jobs on shared resources. |
| OD-04 | Canonical telemetry and diagnostic schemas, timestamp alignment, and model input compatibility | Determines correctness across Fab/Tool integrations. |
| OD-05 | Egress publication semantics and decision on transactional outbox | Determines handling of partial failures and duplicate publication. |
| OD-06 | Supported deployment model, CPU/GPU resource budgets, and scaling policy | Determines operational capacity and isolation. |
| OD-07 | SLOs, alert thresholds, retention, audit access, and disaster recovery | Determines measurable production-readiness targets. |

These are **open design decisions**, not defects in the logical package architecture. They MUST be resolved in the relevant ADRs or implementation contracts before the corresponding production behavior is claimed.

## Appendix A. Reference Package Structure

```text
project/
├── server.py                         # ASGI entrypoint
├── worker.py                         # Background compute entrypoint
├── bootstrap/                        # Composition root (initialization only)
│   ├── config.py
│   ├── container.py
│   ├── plugin_loader.py
│   └── wiring.py
└── src/
    ├── api/                          # Inbound request adapter
    │   ├── routers.py
    │   ├── schemas.py
    │   ├── dependencies.py
    │   ├── auth.py
    │   └── exceptions.py
    ├── services/                     # Platform runtime services
    │   ├── lifecycle.py
    │   ├── resource_manager.py
    │   ├── health.py
    │   ├── observability.py
    │   └── concurrency.py
    ├── application/                  # Business orchestration
    │   ├── use_cases.py
    │   ├── registry.py
    │   ├── hierarchy.py
    │   └── orchestration.py
    ├── domain/                       # Stable domain core
    │   ├── models.py
    │   ├── strategy.py
    │   └── ports.py
    ├── plugins/                      # DS-owned solutions
    │   ├── inference/
    │   ├── heuristics/
    │   └── interceptors/
    ├── infrastructure/               # SWE-owned driven adapters
    │   ├── persistence/
    │   ├── messaging/
    │   ├── locking/
    │   └── downstream/
    ├── config/                       # Optional shared settings
    ├── utils/                        # Pure shared utilities
    └── shared_models/               # Neutral cross-boundary DTOs
```

## Appendix B. Illustrative Dependency Wiring

The following pseudocode explains the architecture only. Identifiers and constructors are illustrative, not claims about implemented interfaces.

```python
# bootstrap/wiring.py — illustrative composition
repository: DataRepositoryPort = DatabricksRepository(client=db_client)
strategy: DiagnosticStrategy = approved_plugin_registry.resolve(configured_id)
publisher: EgressPublisherPort = ExternalReportPublisher(client=egress_client)
lock: DistributedLockPort = RedisLockAdapter(client=redis_client)

use_case = ExecuteDiagnosticUseCase(
    repository=repository,
    strategy=strategy,
    publisher=publisher,
    lock=lock,
)

# API and worker both receive a provider for the same use-case contract.
api_container.register(ExecuteDiagnosticUseCase, provider=use_case_provider)
worker_container.register(ExecuteDiagnosticUseCase, provider=use_case_provider)
```

## Appendix C. Glossary

| Term | Definition |
|---|---|
| Composition Root | The outermost assembly point responsible for creating objects and binding implementations to consumer interfaces. |
| Dependency Injection (DI) | Supplying a dependency from outside a consumer rather than constructing it inside the consumer. |
| Wire / Wiring | Create or register concrete implementations and connect them to declared consumer dependencies. |
| Inbound Adapter | A component that adapts external interactions into application operations, such as an HTTP router. |
| Outbound Port | An abstract capability required by business/application logic and implemented by an external adapter. |
| Plugin | A replaceable, contract-conforming algorithm or customization implementation. |
| Use Case | An application-level operation that coordinates domain contracts and side effects for a specific goal. |
| Process-scoped Resource | An object whose lifetime and memory ownership are limited to a single runtime process. |
| Pure Domain Core | A domain package independent from outer application, runtime, transport, plugin, and infrastructure implementations. |

---

**Document end — PDM-SK-SAD-001 / Version 1.0 / Release Review — Approval Pending**
