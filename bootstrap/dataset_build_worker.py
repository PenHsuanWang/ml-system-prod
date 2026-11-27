"""Poll durable DatasetBuilder work outside HTTP and training processes."""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from bootstrap.dataset_build_wiring import build_worker
from bootstrap.ml_config import MLSettings
from src.domain.ml import Unavailable
from src.infrastructure.ml_identity import MongoActiveActorResolver
from src.infrastructure.persistence.dataset_build_jobs import SQLDatasetBuildJobs
from src.infrastructure.persistence.ml_database import Database


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Claim at most one job and exit")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        settings = MLSettings.from_env()
        database = Database(settings.database_url)
        try:
            database.ready()
            identity = MongoActiveActorResolver(
                os.environ["APP_MONGO_URI"], os.environ.get("APP_MONGO_DATABASE", "backend")
            )
            try:
                worker = build_worker(
                    settings,
                    SQLDatasetBuildJobs(database),
                    Path(os.environ.get("ML_RAW_SNAPSHOT_ROOT", "var/raw-snapshots")),
                    identity.resolve_active,
                )
                while True:
                    try:
                        job_id = worker.run_once()
                        if args.once:
                            print(json.dumps({"job_id": job_id} if job_id else {"idle": True}))
                            return
                    except Unavailable:
                        if args.once:
                            raise
                    time.sleep(1)
            finally:
                identity.close()
        finally:
            database.close()
    except KeyboardInterrupt:
        return
    except Exception:
        print('{"error":"DATASET_BUILD_WORKER_FAILED"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
