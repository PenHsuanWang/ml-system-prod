# WP7 platform usability and acceptance

Baseline `master@e6c257ea366d0aebbdfd14785d9631b089fce926` includes the merge of
PR #22 and its lease and retry corrections. This work is on
`feature/platform-usability-acceptance`.

## Delivered contracts

- Public `GET /api/v1/platform/info` identifies the current service and links
  to its API reference, capabilities and Identity routes when available.
- Authenticated `GET /api/v1/platform/capabilities` filters workflow entry
  points by the service's enabled modules and the actor's current permissions.
  Each action retains its own tenant, resource and approval checks. A listed
  capability can have no resources or an unavailable runtime.
- Management `GET /api/v1/platform/audit-events` requires `ml:operate` and an
  exact subject with an explicit seven-day maximum window. The SQL reader
  filters by authenticated tenant and uses bounded deterministic pages.
- Management and Gateway serve pinned Swagger/ReDoc assets and render paths
  under their configured ASGI prefixes. Optional same-origin Identity links are
  configured by the operator. Only the existing Identity handlers serve those
  requests through the documented proxy allowlist.
- Validation errors use the same request ID as the tracing header. The
  existing status codes, error fields and prediction API remain compatible.

The [quickstart](../ml-runbook.md#self-service-quickstart-wp7) includes role
handoffs, proxy configuration, support correlation and operational limits.
The change reuses the existing Identity service, SQL audit store and active
prediction catalog. Its additive HTTP and packaging changes require no
database migration. To remove same-origin sign-in, unset
`ML_GATEWAY_IDENTITY_PREFIX` and remove the exact proxy locations. A rollback
can restore the previous qualified image and proxy configuration using the
existing database.

## Verification

Verification results will be recorded against the tested implementation.
`production_accepted=false`.
