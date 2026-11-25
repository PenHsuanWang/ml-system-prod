"""Opt-in Management discovery with a dedicated deployment-owned identity."""

import os
from pathlib import Path

from bootstrap.ingestion_wiring import _approved_mapping
from src.application.catalog.service import CatalogService
from src.infrastructure.catalog.databricks_metadata import DatabricksMetadataProvider
from src.infrastructure.catalog.policy import DatabricksDiscoveryPolicy
from src.infrastructure.ingestion.adapters.databricks_contract import parse_connections


def build_catalog() -> CatalogService | None:
    enabled = os.environ.get("ML_CATALOG_ENABLED", "false").lower()
    if enabled not in {"true", "false", "1", "0"}:
        raise ValueError("ML_CATALOG_ENABLED must be an explicit boolean")
    if enabled in {"false", "0"}:
        return None
    if not all(
        os.environ.get(k) for k in ("ML_CATALOG_CONNECTIONS_FILE", "ML_CATALOG_POLICY_FILE")
    ):
        return None
    paths = []
    for key in ("ML_CATALOG_CONNECTIONS_FILE", "ML_CATALOG_POLICY_FILE"):
        path = Path(os.environ.get(key, ""))
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Discovery requires separate approved absolute configuration files")
        paths.append(path)
    connections = parse_connections(_approved_mapping(paths[0]))
    ingestion = os.environ.get("ML_DATABRICKS_CONNECTIONS_FILE")
    worker_connections = parse_connections(_approved_mapping(Path(ingestion))) if ingestion else {}
    for connection in connections.values():
        if connection.access_token_env not in {None, "ML_DISCOVERY_DATABRICKS_TOKEN"}:
            raise ValueError("Discovery requires its reserved dedicated token reference")
        if any(
            connection == worker
            or connection.client_id is not None
            and connection.client_id == worker.client_id
            or connection.client_secret_file is not None
            and connection.client_secret_file == worker.client_secret_file
            or connection.access_token_env is not None
            and connection.access_token_env == worker.access_token_env
            for worker in worker_connections.values()
        ):
            raise ValueError("Discovery and ingestion require distinct credential references")
    policy = DatabricksDiscoveryPolicy(connections, lambda: _approved_mapping(paths[1]))
    return CatalogService(policy, DatabricksMetadataProvider(connections))
