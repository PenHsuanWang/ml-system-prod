"""Isolated API/worker subprocess fixture with a synthetic Databricks byte client."""

import os
import sys
import time

import jwt
import uvicorn
from fastapi import FastAPI

from bootstrap.ingestion_wiring import IngestionSettings
from src.api.ingestion.routers import router as jobs_router
from src.api.ingestion.source_routers import router as source_router
from src.api.ml_http import install_errors
from src.domain.ml import Actor

TEST_KEY = "synthetic-runtime-test-key-at-least-32-bytes"


class SyntheticClient:
    def stream(self, connection, template, limits, control):
        control.check()
        yield b'{"temperature":1.0}\n'


class FixtureIdentity:
    async def resolve(self, token):
        claims = jwt.decode(token, TEST_KEY, algorithms=["HS256"])
        return Actor(claims["tenant"], claims["sub"], frozenset(claims["permissions"]))


def main():
    # Substitute only the external network boundary. SQL catalog, adapter
    # resolution, bounded stager, supervisor and publication remain real.
    from bootstrap import ingestion_wiring
    from src.infrastructure.ingestion.adapters import databricks_client

    databricks_client.DatabricksSQLClient = SyntheticClient
    ingress = ingestion_wiring.build_ingestion(IngestionSettings.from_env())
    if sys.argv[1] == "worker":
        print("READY", flush=True)
        while True:
            ingress.supervisor().run_once()
            time.sleep(0.05)
    app = FastAPI()
    app.state.ml_identity = FixtureIdentity()
    app.state.ingestion_service = ingress.service
    app.state.source_management = ingress.source_management
    app.include_router(jobs_router, prefix="/api/v1/ingestion")
    app.include_router(source_router, prefix="/api/v1/ingestion")
    install_errors(app)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["FIXTURE_PORT"]), log_level="error")


if __name__ == "__main__":
    main()
