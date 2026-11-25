import copy

import pytest

from src.application.catalog.service import CatalogService
from src.domain.catalog.models import MetadataItem
from src.domain.ml import Actor
from src.infrastructure.catalog.policy import DatabricksDiscoveryPolicy
from src.infrastructure.ingestion.adapters.databricks_contract import ConnectionConfig
from tests.ml.ingestion.test_source_management import runtime as runtime


__all__ = ["runtime"]


@pytest.fixture
def discovery():
    actor = Actor.from_identity("tenant", "browser", frozenset({"ml.catalog.read"}))
    config = ConnectionConfig(
        "dbc-synthetic.cloud.databricks.com",
        "/sql/1.0/warehouses/abcd",
        access_token_env="ML_DISCOVERY_DATABRICKS_TOKEN",
    )
    policy_data = {
        "contract": "databricks-discovery-v1",
        "grants": [
            {
                "tenant_id": "tenant",
                "actor_ids": ["browser"],
                "connection_ref": "warehouse",
                "table_scope": ["synthetic", "fdc"],
                "columns": ["temperature", "unsupported"],
            }
        ],
    }
    policy = DatabricksDiscoveryPolicy({"warehouse": config}, lambda: copy.deepcopy(policy_data))

    class Provider:
        calls = []
        values = (MetadataItem("trace"),)

        def fetch(self, connection_ref, query, scopes):
            self.calls.append((connection_ref, query, scopes))
            return self.values

    provider = Provider()
    return CatalogService(policy, provider), actor, policy_data, provider, config
