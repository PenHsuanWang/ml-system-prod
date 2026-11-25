"""Isolated API/worker subprocess fixture with a synthetic Databricks byte client."""

import json
import os
import sys
import time
from pathlib import Path

import jwt
import uvicorn
from fastapi import FastAPI

from bootstrap.ingestion_wiring import IngestionSettings
from src.api.ingestion.routers import router as jobs_router
from src.api.ingestion.source_routers import router as source_router
from src.api.ml_http import install_errors
from src.domain.ml import Actor
from src.infrastructure.ingestion.adapters.databricks_contract import build_query, encode_row

SYNTHETIC_TABLES = {
    ("synthetic", "fdc", "trace_a"): 11.0,
    ("synthetic", "fdc", "trace_b"): 22.0,
}

TEST_KEY = "synthetic-runtime-test-key-at-least-32-bytes"


class SyntheticClient:
    def stream(self, connection, template, limits, control):
        control.check()
        if template.table not in SYNTHETIC_TABLES:
            raise AssertionError("Fixture received an unexpected table")
        query, parameters = build_query(template, limits.max_records)
        event = {
            "worker_id": os.environ["FIXTURE_WORKER_ID"],
            "pid": os.getpid(),
            "table": list(template.table),
            "delta_version": template.delta_version,
            "template_digest": template.digest(connection),
            "query": query,
            "parameters": parameters,
        }
        with Path(os.environ["FIXTURE_TRACE_FILE"]).open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        yield encode_row(template, [SYNTHETIC_TABLES[template.table]], limits.max_line_bytes)


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
        permit = Path(os.environ["FIXTURE_PERMIT_FILE"])
        while True:
            if permit.exists():
                result = ingress.supervisor().run_once()
                if result is not None:
                    # Consume a permit only after a job completes. The parent
                    # waits for removal before granting another worker a job.
                    permit.unlink()
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
