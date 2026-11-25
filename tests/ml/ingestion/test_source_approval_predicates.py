"""Exact scalar types at the mandatory predicate authorization boundary."""

import copy
import json

import pytest

from src.domain.ingestion.models import IngestionRequest, SourceDefinition
from src.domain.ml import Forbidden, canonical_json
from src.infrastructure.ingestion.adapters.databricks_contract import (
    SERIALIZATION_VERSION,
    parse_connections,
    parse_templates,
    validate_source,
)
from src.infrastructure.ingestion.adapters.source_approval import DatabricksSourceApproval
from tests.ml.ingestion import test_source_management

runtime = test_source_management.runtime


@pytest.mark.parametrize(
    ("required_value", "source_value", "accepted"),
    [
        (True, 1, False),
        (1, True, False),
        (False, 0, False),
        (0, False, False),
        (1, 1.0, False),
        (1.0, 1, False),
        (0, 0.0, False),
        (0.0, 0, False),
        (True, True, True),
        (False, False, True),
        (1, 1, True),
        (0, 0, True),
        (1.0, 1.0, True),
        (1.25, 1.25, True),
        ("approved", "approved", True),
        (None, None, True),
    ],
)
def test_mandatory_predicate_authorization_preserves_scalar_types(
    runtime, required_value, source_value, accepted
):
    _, actor, body, settings, policy = runtime
    operator = "is_null" if required_value is None else "eq"
    required = {"column": "temperature", "operator": operator, "value": required_value}
    candidate = {**required, "value": source_value}
    policy = copy.deepcopy(policy)
    policy["grants"][0]["template"]["predicates"] = [required]
    template_data = {**body}
    for key in ("source_id", "version", "allowed_actor_ids"):
        template_data.pop(key)
    template_data["predicates"] = [candidate]
    connections = parse_connections(json.loads(settings.databricks_connections_file.read_text()))
    templates = parse_templates({"typed": template_data})
    template = templates["typed"]
    connection = connections[template.connection_ref]
    # Legacy template resolution retains the input scalar types, including
    # integral floats. This exercises the authorization comparison directly
    # without altering the canonical numeric serialization/digest contract.
    source = SourceDefinition(
        "typed-predicate",
        "v1",
        actor.tenant_id,
        "databricks-sql",
        "1",
        canonical_json(
            {
                "template_ref": "typed",
                "template_digest": template.digest(connection),
                "serialization_version": SERIALIZATION_VERSION,
            }
        ),
        allowed_actor_ids=(actor.actor_id,),
    )
    original_digest = source.configuration_digest
    approval = DatabricksSourceApproval(connections, templates, lambda: policy)
    approved = parse_templates({"approved": policy["grants"][0]["template"]})["approved"]
    # Preserve existing Predicate equality and template fingerprints even when
    # Python considers equal numeric values of different types equivalent.
    assert template.predicates == approved.predicates
    assert (template.digest(connection) == approved.digest(connection)) == (
        canonical_json(source_value) == canonical_json(required_value)
    )
    assert validate_source(source, templates, connections) == template
    if accepted:
        approval.authorize(actor, source)
    else:
        with pytest.raises(Forbidden, match="Source access is not authorized"):
            approval.authorize(actor, source)
    assert source.configuration_digest == original_digest
    # Legacy execution and CLI registration retain their original approval path.
    approval.execution(source, actor.actor_id)
    approval.registration(actor, source)


@pytest.mark.parametrize("value", [True, False, 1, 0, 1.25, "approved", None])
def test_identical_runtime_predicates_are_accepted(runtime, value):
    _, _, body, settings, policy = runtime
    predicate = {
        "column": "temperature",
        "operator": "is_null" if value is None else "eq",
        "value": value,
    }
    policy["grants"][0]["template"]["predicates"] = [predicate]
    settings.databricks_policy_file.write_text(json.dumps(policy))
    source, enabled = test_source_management.register(runtime, {**body, "predicates": [predicate]})
    assert enabled and source.source_id == body["source_id"]


@pytest.mark.parametrize(
    ("source_value", "revoked_value"),
    [(True, 1), (1, True), (False, 0), (0, False), (1, 1.0), (0, 0.0)],
)
def test_runtime_policy_type_change_revokes_admission_and_queued_execution(
    runtime, source_value, revoked_value
):
    ingress, actor, body, settings, policy = runtime
    predicate = {"column": "temperature", "operator": "eq", "value": source_value}
    policy["grants"][0]["template"]["predicates"] = [predicate]
    settings.databricks_policy_file.write_text(json.dumps(policy))
    source, _ = test_source_management.register(runtime, {**body, "predicates": [predicate]})
    job = ingress.service.submit(actor, "typed-before-revocation", IngestionRequest("trace", "v1"))
    assert ingress.sources.for_execution(job) == source
    policy["grants"][0]["template"]["predicates"] = [{**predicate, "value": revoked_value}]
    settings.databricks_policy_file.write_text(json.dumps(policy))
    with pytest.raises(Forbidden):
        ingress.service.submit(actor, "typed-after-revocation", IngestionRequest("trace", "v1"))
    with pytest.raises(Forbidden):
        ingress.sources.for_execution(job)
    stored, _ = ingress.sources.metadata(actor, "trace", "v1")
    assert stored == source
    assert stored.configuration_digest == source.configuration_digest
