# Runtime source management operator walkthrough

Initialize with ml-system init --profile compose or --profile external.
Select Databricks and provide the connection reference, hostname, Warehouse path
and OAuth M2M or token authentication. Accept the default runtime template option.
No table information is required. Existing templates default to keep.
Private file replacements retain the existing confirmation and backup flow.

Before startup, a deployment operator approves tenant/table access in the generated
private databricks/access-policy.json. The default empty policy denies runtime
registration. Further sources within approved scope require no deployment changes.

The following synthetic policy illustrates an approved table scope.

~~~json
{
  "contract": "databricks-access-v1",
  "grants": [{
    "tenant_id": "synthetic-tenant",
    "manager_ids": ["source-operator"],
    "actor_ids": ["ingestion-client"],
    "table_scope": ["manufacturing", "fdc"],
    "template": {
      "connection_ref": "warehouse-main",
      "table": ["manufacturing", "fdc", "process_trace"],
      "delta_version": 128,
      "projection": [{"name": "temperature", "type": "float"}],
      "predicates": [{"column": "temperature", "operator": "ge", "value": 0}]
    },
    "limits": {
      "max_bytes": 16777216,
      "max_records": 100000,
      "max_line_bytes": 1048576,
      "timeout_seconds": 30
    }
  }]
}
~~~

A grant uses exact tenant and connection identity. Optional table_scope is a validated catalog/schema/table prefix with one to three identifiers. Omission restricts access to the exact example table. A two-part scope permits new tables within one approved schema. It permits subsets of
its typed columns and requires all listed predicates. Additional predicates use
AND and narrow the extraction. The grant's delta_version is example metadata.
A source may freeze any structurally valid nonnegative history version within
that table scope. Availability requires separate live qualification.
A source requires a nonempty actor subset of the grant's actor_ids. Manager
authority and execution authority are checked independently.

The wizard emits ML_DATABRICKS_POLICY_CONFIG pointing to this private file and
ML_DATABRICKS_POLICY_FILE=/app/config/databricks-policy.json. Compose mounts it
read-only into Management and the worker. OAuth secrets and tokens remain worker-only.
Existing deployments without those settings retain legacy template operation.
Bootstrap permits an omitted ML_DATABRICKS_SOURCES_FILE. Existing Compose profiles
retain the empty legacy sources.json placeholder for mount compatibility.

Start the system with the command printed by init. Authenticate through the
existing Identity login API as the source operator and ingestion client.
Grant ml.source.manage and ml.ingest respectively. Existing aliases map these to
ml:source:manage and ml:ingest. Use the existing deployment identity administration
to provision those users and permissions. See the
[Identity API contract](microservice-contracts.md). Supply tokens through the
client's secure environment and set BASE_URL to Management.

~~~sh
curl --fail-with-body -H "Authorization: Bearer $SOURCE_OPERATOR_TOKEN" \
  "$BASE_URL/api/v1/ingestion/connections"

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $SOURCE_OPERATOR_TOKEN" -H 'Content-Type: application/json' \
  "$BASE_URL/api/v1/ingestion/sources" -d '{
    "source_id":"fdc-process-trace", "version":"v1",
    "connection_ref":"warehouse-main",
    "table":["manufacturing","fdc","process_trace"], "delta_version":128,
    "projection":[{"name":"temperature","type":"float"}],
    "predicates":[{"column":"temperature","operator":"ge","value":0}],
    "allowed_actor_ids":["ingestion-client"]
  }'

curl --fail-with-body -H "Authorization: Bearer $SOURCE_OPERATOR_TOKEN" \
  "$BASE_URL/api/v1/ingestion/sources?limit=50&offset=0"

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $INGESTION_CLIENT_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: synthetic-extraction-v1' \
  "$BASE_URL/api/v1/ingestion/jobs" \
  -d '{"source_id":"fdc-process-trace","source_version":"v1","max_records":1000}'
~~~

The submission response supplies status_url and result_url. Poll status_url and
GET result_url after PUBLISHED with the ingestion client's token. Results include
source_configuration_digest, content_digest, manifest_digest and source_revision.
A source registration response always reports external_qualification=not_verified.
Registration proves structure and policy authorization. Warehouse authentication,
schema compatibility and Delta history availability require live qualification.

Create v2 with POST /api/v1/ingestion/sources/fdc-process-trace/versions and the same
body without source_id, using version=v2 and the new fixed Delta version.
Jobs referencing v1 retain its original configuration. Disable v1 with POST
/api/v1/ingestion/sources/fdc-process-trace/versions/v1/disable. Published snapshots
retain existing result-read authorization and immutable content.

## API contract

All paths below are under /api/v1/ingestion. Management operations require
authentication, ml:source:manage and matching deployment policy grants.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | /connections | Permitted connection_ref values only |
| POST | /sources | Register source_id plus immutable version, 201 |
| GET | /sources | Authorized metadata page |
| GET | /sources/{source_id}/versions | Authorized version page |
| GET | /sources/{source_id}/versions/{version} | Version metadata including enabled |
| POST | /sources/{source_id}/versions | Register a version, 201 |
| POST | /sources/{source_id}/versions/{version}/enable | Set lifecycle flag, 200 |
| POST | /sources/{source_id}/versions/{version}/disable | Set lifecycle flag, 200 |

Requests accept adapter_type=databricks-sql, connection_ref, three-part table,
fixed delta_version, typed projection, structured predicates, optional limits
and required allowed_actor_ids. Unknown fields are rejected at every DTO level.
Tenant identity comes from the Actor. Digests are generated on the server.

Responses contain source_id, version, adapter_type, configuration_digest,
configuration, limits, allowed_actor_ids, enabled and external_qualification.
Configuration contains the approved typed table settings. Connection endpoint
and credential metadata are excluded.

Equivalent same-version replay returns the same resource with 201, including its
current enabled flag. Different content returns 409. Replay never enables a
disabled resource. Configuration updates require a new immutable version.

Lists use limit=1..100, default 50, and offset=0..10000, default 0.
Ordering is source_id and version lexicographically. SQL pagination precedes
policy filtering. next_offset=offset+limit advances the scanned page, including
empty policy-filtered pages. Stop at the offset ceiling. Concurrent registration
can shift offset pages. Snapshot-consistent pagination is not promised.
Disabled sources remain queryable to authorized managers.

Existing sanitized error envelopes return 401 for missing authentication,
403 for missing grants, 404 for absent tenant resources, 409 for immutable conflicts,
422 for invalid HTTP DTOs, 400 for invalid domain contracts and 503 for unconfigured
source management. Existing job and snapshot APIs retain their own permissions.

Audits retain actor, tenant, action and timestamp. Every version registration uses
ingestion.source.register. Lifecycle actions use ingestion.source.enable/disable.
Join audit subject_id to the tenant's source configuration_digest for exact
immutable source identity. Audit events never include credentials or table data.

## Revocation and compatibility

Admission and acquisition recheck enablement, actor grants, immutable digest,
current policy and public connection identity. Policies are bounded JSON read on
each check. Deployment administration owns policy changes and cross-instance
propagation. A single-file Docker bind mount may retain an old inode across
atomic host replacement. Update the mounted inode or recreate the mount during
policy administration. Runtime source registration requires no policy refresh.

Disablement does not promise immediate remote query cancellation. Queued/retried
acquisition rejects revoked grants. Existing durable publication intents reconcile
their staged candidate under the existing recovery contract. Historical snapshots
are retained.

Legacy template sources retain their fingerprints, static resolution and CLI
management commands. New HTTP management of a legacy source additionally requires
a matching policy grant. File Batch and downstream dataset/training contracts
retain their existing paths.

Kubernetes engineering acceptance retains its File Batch profile. A Databricks
deployment must separately mount public connections, optional legacy templates
and policy into the applicable roles, with worker-only credentials. Live
Databricks Kubernetes qualification remains separate.

See [ADR-022](adr/022-runtime-data-source-management.md) and the
[implementation report](reviews/runtime-source-management-report.md).

## Synthetic request and response examples

Authenticate each pre-provisioned principal through the existing login endpoint.
The following credentials illustrate a disposable test deployment.

~~~sh
curl --fail-with-body -H 'Content-Type: application/json' \
  "$BASE_URL/api/v1/identity/login" \
  -d '{"tenant_id":"synthetic-tenant","username":"source-operator","password":"synthetic-example-only"}'
~~~

Use the returned access_token for SOURCE_OPERATOR_TOKEN. Authenticate the
ingestion-client principal separately for INGESTION_CLIENT_TOKEN.

A registration response has the following shape. Digest text is illustrative.

~~~json
{
  "source_id": "fdc-process-trace",
  "version": "v1",
  "adapter_type": "databricks-sql",
  "configuration_digest": "server-generated-sha256",
  "configuration": {
    "connection_ref": "warehouse-main",
    "table": ["manufacturing", "fdc", "process_trace"],
    "delta_version": 128,
    "projection": [{"name": "temperature", "type": "float"}],
    "predicates": [{"column": "temperature", "operator": "ge", "value": 0}]
  },
  "limits": {
    "max_bytes": 16777216,
    "max_records": 100000,
    "max_line_bytes": 1048576,
    "timeout_seconds": 30
  },
  "allowed_actor_ids": ["ingestion-client"],
  "enabled": true,
  "external_qualification": "not_verified"
}
~~~

Retrieve the job result using the job_id returned by submission, then create v2
and disable v1 with the following requests.

~~~sh
curl --fail-with-body -H "Authorization: Bearer $INGESTION_CLIENT_TOKEN" \
  "$BASE_URL/api/v1/ingestion/jobs/$JOB_ID"
curl --fail-with-body -H "Authorization: Bearer $INGESTION_CLIENT_TOKEN" \
  "$BASE_URL/api/v1/ingestion/jobs/$JOB_ID/result"

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $SOURCE_OPERATOR_TOKEN" -H 'Content-Type: application/json' \
  "$BASE_URL/api/v1/ingestion/sources/fdc-process-trace/versions" -d '{
    "version":"v2", "connection_ref":"warehouse-main",
    "table":["manufacturing","fdc","process_trace"], "delta_version":129,
    "projection":[{"name":"temperature","type":"float"}],
    "predicates":[{"column":"temperature","operator":"ge","value":0}],
    "allowed_actor_ids":["ingestion-client"]
  }'
curl --fail-with-body -X POST -H "Authorization: Bearer $SOURCE_OPERATOR_TOKEN" \
  "$BASE_URL/api/v1/ingestion/sources/fdc-process-trace/versions/v1/disable"
~~~
