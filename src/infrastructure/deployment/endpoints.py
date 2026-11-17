"""Controller-owned endpoint discovery; no Kubernetes token is needed by Gateway."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from src.domain.deployments.identity import deployment_spec
from src.domain.deployments.models import Deployment
from src.domain.ml import Conflict


def resource_name(deployment: Deployment) -> str:
    return "ml-serve-" + UUID(deployment.deployment_id).hex


class KubernetesEndpointResolver:
    def __init__(self, namespace: str) -> None:
        if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace) is None:
            raise ValueError("Invalid Serving namespace")
        self.namespace = namespace

    def resolve(self, deployment: Deployment) -> str:
        deployment_spec(deployment)
        return f"http://{resource_name(deployment)}.{self.namespace}.svc:8001"


class LocalEndpointResolver:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, deployment: Deployment) -> str:
        resource_name(deployment)  # Validate path identity before accessing local files.
        deployment_spec(deployment)
        address = json.loads((self.root / deployment.deployment_id / "instance.json").read_text())[
            "address"
        ]
        uri = urlsplit(address)
        if (
            uri.scheme != "http"
            or uri.hostname != "127.0.0.1"
            or not uri.port
            or uri.username
            or uri.password
            or uri.path
            or uri.query
            or uri.fragment
        ):
            raise Conflict("Invalid local Serving address")
        return address
