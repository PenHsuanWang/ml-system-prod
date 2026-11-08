"""Explicit ingress operations; workers poll SQL and never run in API handlers."""

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.ingestion.models import SourceDefinition
from src.domain.ml import Actor, MLError


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("health")
    commands.add_parser("templates")
    register = commands.add_parser("register-source")
    register.add_argument("--definition-path", type=Path, required=True)
    register.add_argument("--tenant-id", required=True)
    for operation in ("enable-source", "disable-source"):
        command = commands.add_parser(operation)
        command.add_argument("--tenant-id", required=True)
        command.add_argument("--source-id", required=True)
        command.add_argument("--source-version", required=True)
    worker = commands.add_parser("work")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--job-id")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--minimum-age-seconds", type=float, default=86400)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--tenant-id", required=True)
    reconcile.add_argument("--job-id", required=True)
    args = parser.parse_args()
    settings = IngestionSettings.from_env()
    components = build_ingestion(settings)
    try:
        if args.operation == "health":
            from sqlalchemy import inspect

            components.database.ready()
            required = {"ingestion_jobs", "ingestion_source_definitions", "raw_snapshots"}
            if not required <= set(inspect(components.database.engine).get_table_names()):
                raise MLError("Ingestion schema is not ready")
            print(json.dumps({"ready": True, "databricks_enabled": settings.databricks_enabled}))
        elif args.operation == "templates":
            print(
                json.dumps(
                    {
                        "templates": [
                            {"template_ref": key, "template_digest": value}
                            for key, value in components.approved_template_digests
                        ]
                    }
                )
            )
        elif args.operation == "register-source":
            import os

            from src.infrastructure.ingestion.storage.raw_snapshot_writer import relative_file_fd

            with relative_file_fd(args.definition_path.parent, args.definition_path.name) as fd:
                with os.fdopen(os.dup(fd), "rb") as stream:
                    definition = stream.read(65537)
            if len(definition) > 65536:
                raise ValueError("Approved source definition exceeds its size limit")
            source = SourceDefinition.parse(json.loads(definition))
            if source.tenant_id != args.tenant_id:
                raise ValueError("Source definition tenant does not match the operation")
            components.registry.validate_source(source)
            actor = Actor(args.tenant_id, "ingestion-operator", frozenset({"ml:source:manage"}))
            components.sources.register(actor, source)
            print(
                json.dumps(
                    {
                        "source_id": source.source_id,
                        "version": source.version,
                        "configuration_digest": source.configuration_digest,
                    }
                )
            )
        elif args.operation in {"enable-source", "disable-source"}:
            actor = Actor(args.tenant_id, "ingestion-operator", frozenset({"ml:source:manage"}))
            components.sources.set_enabled(
                actor, args.source_id, args.source_version, args.operation == "enable-source"
            )
        elif args.operation == "reconcile":
            actor = Actor(args.tenant_id, "ingestion-operator", frozenset({"ml:ingestion:operate"}))
            reconciled = components.service.reconcile(actor, args.job_id)
            print(json.dumps({"job_id": reconciled.job_id, "state": reconciled.state}))
        elif args.operation == "cleanup":
            if args.minimum_age_seconds < 1:
                parser.error("Cleanup requires a positive minimum age")
            removed = components.store.cleanup_abandoned(
                components.jobs.protected_job_ids(), time.time() - args.minimum_age_seconds
            )
            print(json.dumps({"removed_private_staging_attempts": removed}))
        else:

            def stop_worker(signum: int, frame: object) -> None:
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, stop_worker)
            supervisor = components.supervisor()
            while True:
                try:
                    job = supervisor.run_once(job_id=args.job_id)
                    if args.once:
                        print(
                            json.dumps(
                                {"job_id": job.job_id, "state": job.state}
                                if job
                                else {"idle": True}
                            )
                        )
                        return
                except MLError:
                    # Durable leases/intents remain available. Never print source/SDK details.
                    if args.once:
                        raise MLError("Ingestion worker could not persist progress") from None
                time.sleep(settings.poll_seconds)
    finally:
        components.close()


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        return
    except Exception:
        print('{"error":"INGESTION_COMMAND_FAILED"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
