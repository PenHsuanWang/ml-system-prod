from dataclasses import replace

import pytest

from src.domain.catalog.models import MetadataItem, MetadataQuery
from src.domain.ml import Forbidden


def test_independent_permissions_and_connections(discovery):
    service, actor, _, provider, _ = discovery
    assert service.connections(actor) == ["warehouse"]
    for permissions in ({"ml:source:manage"}, {"ml:ingest"}, set()):
        with pytest.raises(Forbidden):
            service.connections(replace(actor, permissions=frozenset(permissions)))
    assert not actor.permissions.intersection({"ml:source:manage", "ml:ingest"})
    for other in (replace(actor, tenant_id="other"), replace(actor, actor_id="other")):
        assert service.connections(other) == []
        with pytest.raises(Forbidden):
            service.browse(other, "warehouse", MetadataQuery("catalogs"))
    assert provider.calls == []


@pytest.mark.parametrize(
    "connection,path",
    [
        ("unknown", ("synthetic", "fdc")),
        ("warehouse", ("private", "fdc")),
        ("warehouse", ("synthetic", "private")),
    ],
)
def test_scope_denied_before_external_call(discovery, connection, path):
    service, actor, _, provider, _ = discovery
    with pytest.raises(Forbidden):
        service.browse(actor, connection, MetadataQuery("tables", path))
    assert provider.calls == []


def test_post_query_scope_and_column_filtering(discovery):
    service, actor, policy, provider, _ = discovery
    policy["grants"][0]["table_scope"] += ["trace"]
    provider.values = (MetadataItem("private"), MetadataItem("trace"))
    page = service.browse(
        actor, "warehouse", MetadataQuery("tables", ("synthetic", "fdc"), limit=1)
    )
    assert page.items == () and page.has_more and page.next_offset == 1
    provider.values = (MetadataItem("temperature"), MetadataItem("salary"))
    page = service.browse(
        actor, "warehouse", MetadataQuery("columns", ("synthetic", "fdc", "trace"))
    )
    assert [i.name for i in page.items] == ["temperature"]
    assert page.version_discovery == "unavailable"


def test_revocation_during_call_discloses_no_response(discovery):
    service, actor, data, provider, _ = discovery

    def revoke(*args):
        data["grants"] = []
        return (MetadataItem("trace"),)

    provider.fetch = revoke
    with pytest.raises(Forbidden):
        service.browse(actor, "warehouse", MetadataQuery("tables", ("synthetic", "fdc")))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(contract="databricks-access-v1"),
        lambda d: d["grants"][0].update(columns=[]),
        lambda d: d["grants"][0].update(table_scope=["cat;DROP"]),
        lambda d: d["grants"][0].update(secret="hidden"),
    ],
)
def test_malformed_policy_denied(discovery, mutate):
    service, actor, data, provider, _ = discovery
    mutate(data)
    with pytest.raises(Forbidden, match="policy is unavailable"):
        service.connections(actor)
    assert provider.calls == []
