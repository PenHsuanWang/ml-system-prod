"""Executed inside the local control container by the foundation verifier."""

import json
import os
from uuid import uuid4

import requests

from bootstrap.deployment_entrypoint import load_runtime_environment


def main() -> None:
    environment = load_runtime_environment(os.environ)
    uri = environment["ML_TRACKING_URI"]
    if uri != "https://mlflow:8443":
        raise ValueError("Foundation smoke is restricted to the local MLflow Service")
    certificate = environment["MLFLOW_TRACKING_SERVER_CERT_PATH"]
    writer = (environment["MLFLOW_TRACKING_USERNAME"], environment["MLFLOW_TRACKING_PASSWORD"])
    reader = (environment["ML_SERVING_MLFLOW_USERNAME"], environment["ML_SERVING_MLFLOW_PASSWORD"])
    session = requests.Session()
    session.trust_env = False
    session.verify = certificate

    def post(path, body, identity=None):
        return session.post(uri + "/api/2.0/mlflow/" + path, json=body, auth=identity, timeout=15)

    assert post("experiments/search", {"max_results": 1}).status_code == 401
    try:
        session.get(uri + "/health", verify=True, timeout=10)
    except requests.exceptions.SSLError:
        pass
    else:
        raise ValueError("Untrusted test CA was unexpectedly accepted")
    assert session.get(uri + "/health", timeout=10).status_code == 200
    existing = session.get(
        uri + "/api/2.0/mlflow/users/get", params={"username": reader[0]}, auth=writer, timeout=15
    )
    if existing.status_code == 404:
        assert (
            post("users/create", {"username": reader[0], "password": reader[1]}, writer).status_code
            == 200
        )
    else:
        assert existing.status_code == 200
    response = post("experiments/create", {"name": "foundation-smoke-" + uuid4().hex}, writer)
    assert response.status_code == 200
    experiment = response.json()["experiment_id"]
    response = session.get(
        uri + "/api/2.0/mlflow/experiments/get",
        params={"experiment_id": experiment},
        auth=reader,
        timeout=15,
    )
    assert response.status_code == 200
    assert (
        post(
            "experiments/update",
            {"experiment_id": experiment, "new_name": "must-not-write"},
            reader,
        ).status_code
        == 403
    )
    for address in ("http://ml-management:8000/health/ready", "http://127.0.0.1:8001/health/ready"):
        response = session.get(address, timeout=10)
        assert response.status_code == 200 and response.json()["ready"] is True
    print(
        json.dumps(
            {
                "status": "FOUNDATION_SMOKE_PASSED",
                "management_ready": True,
                "gateway_ready": True,
                "mlflow_tls_verified": True,
                "untrusted_ca_rejected": True,
                "unauthenticated_rejected": True,
                "reader_read_allowed": True,
                "reader_experiment_write_denied": True,
                "retained_experiment_id": experiment,
                "production_qualified": False,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Foundation smoke failed; retain private diagnostics without printing credentials"
        ) from None
