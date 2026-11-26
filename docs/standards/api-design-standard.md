# API documentation and contract standard

Applies to new or explicitly revised APIs. Existing wire contracts are preserved through compatibility review.

| Topic | Required contract |
| --- | --- |
| Summary and description | English action-oriented summary, purpose, prerequisites, related operations, required permissions, tenant and ownership rules. Give a first-use sequence. Mark proposed operations as PLANNED outside live OpenAPI. |
| Operation identity | Stable unique operation IDs. Record changes that affect generated clients. Existing IDs should survive documentation-only changes. |
| Authentication | Declare the actual OpenAPI security scheme. Explain trusted login URL, token expiry and revocation. Authentication failures and permission denial have separate semantics. Derive resource tenant from Identity. |
| Request DTO | Typed fields with required/default semantics, ranges, finite-number rules, units or explicit unknown units, ordering, batch/body limits, extra-field policy and supported schema versions. No caller-defined code, internal resource locations or tenant bypass. |
| Response DTO | Describe fields, lifecycle observations, immutable version/digest and provenance. Validate actual successful HTTP bodies. JSONResponse bypasses FastAPI response_model validation, so explicitly validate at a reviewed boundary or record the compatibility limitation. Preserve original JSON on transport pass-through when validation is added. |
| Examples | Synthetic examples pass the request DTO and, where feasible, an isolated runtime. State when endpoint IDs must be replaced. Include representative success and errors. Avoid secrets and actual enterprise payloads. |
| Error contract | Document actual status, code, body fields, correlation header and retryable flag. Proxied errors require runtime tests. Keep status/code/shape compatibility. Management Identity HTTPException uses detail, ML domain errors use code/message/request_id/retryable, and payload-limit errors omit request_id. Record route-specific exceptions. |
| Request ID | Explain X-Request-ID header generation/forwarding and body ID differences. Never promise a persisted prediction history without storage evidence. Trace and logging exclude token and payload content. |
| Idempotency | State scope, fingerprint, retention and conflict for existing keys. Async creation describes status/result URLs and recovery. Prediction currently has no replay contract. Retry may repeat work and select a newer deployment. |
| Pagination | New collections need bounded SQL-side queries, deterministic ordering and permitted filters. Default 25/max 100 unless justified. Preserve existing envelope and pagination on old routes. If using cursors, bind tenant/query, check integrity/expiry, reauthorize each page and describe concurrent changes. |
| Lifecycle | Distinguish accepted, ready, active, failed, cancelled and immutable published states. Specify transition authority and revocation behavior. SQL active route and observed runtime readiness have separate timestamps and meaning. |
| Version compatibility | Keep resource identity, registered version, schema version and deployment generation distinct. Additive fields require compatible clients. Breaking field/shape/preprocessing/output semantics require a migration and versioned rollout. Define legacy incomplete metadata behavior. |
| Sensitive information | Apply tenant/ownership/RBAC on list and detail as well as writes. Allowlist public metadata. Exclude artifact URIs, internal addresses, credential paths, stack traces and raw enterprise data. Limit responses and sanitize errors. |
| Contract evidence | Extract OpenAPI from the actual factory, exercise authentication, negative paths and proxy root_path, validate actual MLP/EBM responses, and assert operation IDs. Include direct Response bypass regression and failure/retry tests. Boundaries and repository CI scopes remain mandatory. |

## Prediction-specific established behavior

Gateway domain failures include 401, 403, 404, 503. JSON Serving errors can propagate 429 and 504.
The Gateway HTTP transport timeout maps to 503. Malformed upstream successful bodies map to 503 after
the WP1 response check. Error bodies preserve existing pass-through behavior.
413 uses the correlation header. The shared 422 handler generates its own body ID, so the header is
the correlation authority for validation failures. Harmonizing the shared handler requires a
separate review.

Live prediction documentation describes current MLP and EBM contracts. Future endpoint catalog
operations stay in the [WP2 design](../plans/prediction-endpoint-discovery.md).
