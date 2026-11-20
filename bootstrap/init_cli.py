"""Interactive, offline setup for the existing local and external Compose profiles."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shlex
import sys
import warnings
from collections.abc import Callable, Set
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

from bootstrap.init_config import (
    DATABRICKS_ENV_KEYS,
    INGESTION_ENV_KEYS,
    LOCAL_PASSWORDS,
    OAUTH_CONTAINER_PATH,
    ROLES,
    SetupPlan,
    SetupSpec,
    build_plan,
    connection_uri,
    credential,
    example_environment,
    https_uri,
    jwt_secret,
    load_object,
    local_defaults,
    single_line,
    validate_production_env,
)
from bootstrap.init_files import (
    PrivateFiles,
    SetupError,
    absolute_path,
    env_values,
    no_links,
    read_regular,
    referenced_keys,
)
from src.domain.ingestion.models import identifier
from src.domain.ml import MLError
from src.infrastructure.ingestion.adapters.databricks_contract import (
    parse_connections,
    parse_templates,
)


class Cancelled(Exception):
    pass


class SetupArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "Invalid setup arguments. Use --help; secrets belong in hidden prompts.\n")


def _uri(schemes: set[str]) -> Callable[[str], str]:
    return lambda value: connection_uri(value, schemes)


class Prompt:
    def ask(
        self,
        label: str,
        default: str | None = None,
        *,
        secret: bool = False,
        validate: Callable[[str], object] = single_line,
        allow_empty: bool = False,
    ) -> str:
        suffix = (
            (" [Enter keeps stored/generated value]" if secret else f" [{default}]")
            if default
            else ""
        )
        while True:
            if secret:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass(label + suffix + ": ")
            else:
                value = input(label + suffix + ": ").strip()
            value = value if value else default or ""
            if not value and allow_empty:
                return ""
            try:
                validate(value)
                return value
            except (SetupError, MLError, ValueError, TypeError, OSError):
                print("Invalid value for this field; please try again.", file=sys.stderr)

    def choose(self, label: str, options: tuple[str, ...], default: str) -> str:
        def validate(value: str) -> None:
            if value.lower() not in options:
                raise SetupError("Choose one of the listed options.")

        return self.ask(
            label + " (" + "/".join(options) + ")",
            default,
            validate=validate,
        ).lower()

    def yes(self, label: str, *, default: bool = False) -> bool:
        return self.choose(label, ("y", "n"), "y" if default else "n") == "y"


def _saved_environment(files: PrivateFiles, path: Path, managed: Set[str]) -> dict[str, str]:
    original = files.read(path)
    if original:
        text = original.decode()
        if referenced_keys(text) & managed:
            raise SetupError(
                "Resolve variable references in managed settings before initialization."
            )
        return env_values(text)
    return {}


def _saved_json(files: PrivateFiles, path: Path, alternate: str | None = None) -> dict[str, Any]:
    content = files.read(path)
    if content is None and alternate:
        content = read_regular(absolute_path(alternate), private=True)
    return load_object(content) if content is not None else {}


def _sources(prompt: Prompt, alias: str, previous: dict[str, Any]) -> dict[str, Any]:
    mode = prompt.choose(
        "Source templates", ("keep", "import", "create", "empty"), "keep" if previous else "empty"
    )
    if mode == "keep":
        return previous
    if mode == "empty":
        return {}
    if mode == "import":

        def approved(value: str) -> dict[str, Any]:
            result = load_object(read_regular(absolute_path(value)))
            parse_templates(result)
            return result

        value = prompt.ask("Approved sources JSON file", validate=approved)
        return approved(value)
    ref = prompt.ask("Template reference", "approved-source-v1", validate=identifier)
    table = [
        prompt.ask(
            label,
            validate=lambda v: (
                bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", v))
                or (_ for _ in ()).throw(ValueError())
            ),
        )
        for label in ("Catalog", "Schema", "Table")
    ]
    delta = prompt.ask(
        "Fixed Delta version",
        validate=lambda v: 0 <= int(v) < 2**63 or (_ for _ in ()).throw(ValueError()),
    )
    projection = []
    while True:
        name = prompt.ask(
            "Projection column name",
            validate=lambda v: (
                bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", v))
                or (_ for _ in ()).throw(ValueError())
            ),
        )
        kind = prompt.choose(
            "Column type",
            ("boolean", "integer", "float", "decimal", "string", "date", "timestamp", "binary"),
            "string",
        )
        projection.append({"name": name, "type": kind})
        if len(projection) >= 128 or not prompt.yes("Add another projection column?"):
            break
    result = {
        ref: {
            "connection_ref": alias,
            "table": table,
            "delta_version": int(delta),
            "projection": projection,
            "predicates": [],
        }
    }
    parse_templates(result)
    return result


def _databricks(
    spec: SetupSpec, files: PrivateFiles, prompt: Prompt, saved: dict[str, str]
) -> None:
    previous = _saved_json(
        files,
        spec.private / "databricks/connections.json",
        saved.get("ML_DATABRICKS_CONNECTIONS_CONFIG"),
    )
    if len(previous) > 1:
        raise SetupError(
            "This wizard manages one Warehouse connection; "
            "retain multi-connection configurations manually."
        )
    if previous:
        parse_connections(previous)
    alias = prompt.ask(
        "Databricks connection reference",
        next(iter(previous), "warehouse-main"),
        validate=identifier,
    )
    current: dict[str, Any] = next(iter(previous.values()), {})
    mode = prompt.choose(
        "Databricks authentication",
        ("oauth", "token"),
        "token" if "access_token_env" in current else "oauth",
    )
    connection = dict(
        server_hostname=current.get("server_hostname", ""), http_path=current.get("http_path", "")
    )

    def valid_connection(key: str, value: str) -> None:
        single_line(value)
        candidate = {**connection, key: value, "access_token_env": "DATABRICKS_TOKEN"}
        # Validate each field independently while the other has not been entered yet.
        candidate.setdefault("server_hostname", "")
        candidate.setdefault("http_path", "")
        if not candidate["server_hostname"]:
            candidate["server_hostname"] = "dbc-approved.cloud.databricks.com"
        if not candidate["http_path"]:
            candidate["http_path"] = "/sql/1.0/warehouses/0"
        parse_connections({alias: candidate})

    for key, label in (
        ("server_hostname", "Workspace hostname (without https://)"),
        ("http_path", "Warehouse HTTP path"),
    ):
        connection[key] = prompt.ask(
            label, connection[key] or None, validate=partial(valid_connection, key)
        )
    if mode == "token":
        connection["access_token_env"] = "DATABRICKS_TOKEN"
        spec.environment["DATABRICKS_TOKEN"] = prompt.ask(
            "Databricks access token",
            saved.get("DATABRICKS_TOKEN"),
            secret=True,
            validate=credential,
        )
    else:
        connection["client_id"] = prompt.ask(
            "OAuth client ID",
            current.get("client_id"),
            validate=lambda v: parse_connections(
                {alias: {**connection, "client_id": v, "client_secret_file": OAUTH_CONTAINER_PATH}}
            ),
        )
        connection["client_secret_file"] = OAUTH_CONTAINER_PATH
        secret_path = spec.private / "databricks/oauth-secret"
        secret = files.read(secret_path)
        alternate = saved.get("ML_DATABRICKS_CLIENT_SECRET_FILE")
        if secret is None and alternate and alternate != "/dev/null":
            secret = read_regular(absolute_path(alternate), private=True)
        spec.oauth_secret = prompt.ask(
            "OAuth client secret",
            secret.decode().rstrip("\r\n") if secret else None,
            secret=True,
            validate=credential,
        )
    spec.connections = {alias: connection}
    prior_sources = _saved_json(
        files, spec.private / "databricks/sources.json", saved.get("ML_DATABRICKS_SOURCES_CONFIG")
    )
    spec.templates = _sources(prompt, alias, prior_sources)
    if spec.profile == "compose":
        spec.environment["ML_INGESTION_INPUT_DIR"] = str(
            absolute_path(
                prompt.ask(
                    "Approved input directory (outside runtime storage)",
                    saved.get("ML_INGESTION_INPUT_DIR", str(spec.private / "ingestion-input")),
                    validate=absolute_path,
                )
            )
        )


def _external(spec: SetupSpec, files: PrivateFiles, prompt: Prompt, saved: dict[str, str]) -> None:
    public = spec.environment
    public["ML_RELEASE_IMAGE"] = prompt.ask(
        "Approved release image@sha256",
        saved.get("ML_RELEASE_IMAGE"),
        validate=lambda v: (
            bool(re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", v))
            or (_ for _ in ()).throw(ValueError())
        ),
    )
    public["ML_IMAGE_DIGEST"] = public["ML_RELEASE_IMAGE"].split("@", 1)[1]
    public["ML_CODE_REVISION"] = prompt.ask(
        "Approved image source revision (40 hex)",
        saved.get("ML_CODE_REVISION"),
        validate=lambda v: (
            bool(re.fullmatch(r"[a-f0-9]{40}", v)) or (_ for _ in ()).throw(ValueError())
        ),
    )
    public["ML_EXTERNAL_MLFLOW_URI"] = prompt.ask(
        "MLflow HTTPS endpoint", saved.get("ML_EXTERNAL_MLFLOW_URI"), validate=https_uri
    )
    public["ML_MLFLOW_CA_FILE"] = str(
        absolute_path(
            prompt.ask(
                "Approved MLflow CA PEM file",
                saved.get("ML_MLFLOW_CA_FILE"),
                validate=lambda v: read_regular(absolute_path(v)),
            )
        )
    )
    public["ML_RUNTIME_ROOT"] = str(
        absolute_path(
            prompt.ask(
                "Runtime storage directory",
                saved.get("ML_RUNTIME_ROOT", str(spec.project / "var")),
                validate=absolute_path,
            )
        )
    )
    public["ML_OPERATOR_DEFINITIONS_DIR"] = str(
        absolute_path(
            prompt.ask(
                "Operator SourceDefinition directory",
                saved.get(
                    "ML_OPERATOR_DEFINITIONS_DIR", str(spec.private / "operator-definitions")
                ),
                validate=absolute_path,
            )
        )
    )
    public.update(ML_UID=str(os.geteuid()), ML_GID=str(os.getegid()))
    public["APP_MONGO_DATABASE"] = prompt.ask(
        "Identity database",
        public.get("APP_MONGO_DATABASE", "ml_identity"),
        validate=lambda v: (
            bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", v))
            or (_ for _ in ()).throw(ValueError())
        ),
    )
    for key in ("ML_MANAGEMENT_PORT", "ML_PREDICTION_PORT"):
        public[key] = prompt.ask(
            key,
            public[key],
            validate=lambda v: 1 <= int(v) <= 65535 or (_ for _ in ()).throw(ValueError()),
        )
    if prompt.yes("Review resource, concurrency and deadline defaults (not capacity approval)?"):
        for key in sorted(public):
            if key.endswith(("_CPUS", "_MEMORY", "_SECONDS", "_MS")) or key in {
                "ML_RUNTIME_PIDS_LIMIT",
                "ML_API_CONCURRENCY",
                "ML_GATEWAY_CONCURRENCY",
                "ML_MAX_PENDING_JOBS_PER_TENANT",
                "ML_QUEUE_NAME",
            }:
                public[key] = prompt.ask(key, public[key])
    policy = prompt.ask(
        "Approved EBM policy JSON file (blank retains current/unqualified policy)", allow_empty=True
    )
    if policy:
        public["ML_EBM_QUALITY_POLICY_JSON"] = json.dumps(
            load_object(read_regular(absolute_path(policy)))
        )
    previous_values: dict[str, str] = {}
    for role in ROLES:
        alternate = (
            str(absolute_path(saved["ML_DEPLOYMENT_CONFIG_DIR"]) / (role + ".json"))
            if saved.get("ML_DEPLOYMENT_CONFIG_DIR")
            else None
        )
        previous = _saved_json(files, spec.private / "roles" / (role + ".json"), alternate)
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in previous.items()):
            raise SetupError("Private role fields must be nonempty strings.")
        spec.existing_roles[role] = previous
        for key, value in previous.items():
            previous_values.setdefault(key, value)
    for key, schemes, label in (
        ("ML_DATABASE_URL", {"postgresql+psycopg"}, "App PostgreSQL URI"),
        ("ML_RABBITMQ_URL", {"amqp", "amqps"}, "RabbitMQ URI"),
        ("ML_REDIS_URL", {"redis", "rediss"}, "Redis URI"),
        ("APP_MONGO_URI", {"mongodb", "mongodb+srv"}, "Mongo Identity URI"),
    ):
        spec.private_values[key] = prompt.ask(
            label + " (hidden; changing updates roles)",
            previous_values.get(key),
            secret=True,
            validate=_uri(schemes),
        )
    spec.private_values["APP_JWT_SECRET"] = prompt.ask(
        "JWT signing secret",
        previous_values.get("APP_JWT_SECRET") or secrets.token_hex(32),
        secret=True,
        validate=jwt_secret,
    )
    spec.changed_private = {
        k for k, v in spec.private_values.items() if previous_values.get(k) != v
    }
    for prefix, label in (
        ("MLFLOW_TRACKING_", "MLflow writer"),
        ("ML_SERVING_MLFLOW_", "MLflow reader"),
    ):
        token_key = prefix + "TOKEN"
        username_key = prefix + "USERNAME"
        password_key = prefix + "PASSWORD"
        saved_mode = "token" if previous_values.get(token_key) else "basic"
        saved_keys = (token_key,) if saved_mode == "token" else (username_key, password_key)
        saved_credentials = next(
            (
                values
                for values in spec.existing_roles.values()
                if all(values.get(key) for key in saved_keys)
            ),
            None,
        )
        mode = prompt.choose(
            label + " authentication",
            ("keep", "basic", "token") if saved_credentials else ("basic", "token"),
            "keep" if saved_credentials else saved_mode,
        )
        if mode == "keep" and saved_credentials is not None:
            # Use one stored credential set as the default for roles without credentials.
            spec.private_values.update({key: saved_credentials[key] for key in saved_keys})
            continue
        if mode == "token":
            spec.private_values[token_key] = prompt.ask(
                label + " token", previous_values.get(token_key), secret=True
            )
            spec.changed_private.add(token_key)
        else:
            spec.private_values[username_key] = prompt.ask(
                label + " username", previous_values.get(username_key)
            )
            spec.private_values[password_key] = prompt.ask(
                label + " password", previous_values.get(password_key), secret=True
            )
            spec.changed_private.update((username_key, password_key))


def configure(project: Path, private: Path, profile: str, prompt: Prompt) -> SetupPlan:
    if os.geteuid() == 0:
        raise SetupError("Run setup as a non-root runtime user.")
    no_links(private)
    if private.is_relative_to(project) and not private.is_relative_to(project / ".local/ml-system"):
        raise SetupError(
            "Inside the checkout use .local/ml-system; custom private roots must be outside it."
        )
    if private.exists() and (
        not private.is_dir()
        or private.stat().st_uid != os.geteuid()
        or private.stat().st_mode & 0o077
    ):
        raise SetupError("The private output directory must be owned by this user with mode 0700.")
    if not (project / "pyproject.toml").is_file() or not (project / "compose.ml.yaml").is_file():
        raise SetupError("Select the repository checkout with --project-dir.")
    files = PrivateFiles(project)
    spec = SetupSpec(project, private, profile)
    external_keys = (
        set(env_values(read_regular(project / ".env.ml.production.example").decode()))
        | DATABRICKS_ENV_KEYS
    )
    local_keys = {
        *LOCAL_PASSWORDS,
        "APP_JWT_SECRET",
        "ML_UID",
        "ML_GID",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    }
    saved = _saved_environment(
        files,
        project / (".env.ml" if profile == "compose" else ".env.ml.production"),
        local_keys if profile == "compose" else external_keys,
    )
    if profile == "compose":
        spec.environment = local_defaults(saved)
        otlp = prompt.ask(
            "Optional OTLP HTTP collector URL",
            saved.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            allow_empty=True,
            validate=lambda v: connection_uri(v, {"http", "https"}),
        )
        spec.environment["OTEL_EXPORTER_OTLP_ENDPOINT"] = otlp
        ingestion = _saved_environment(files, project / ".env.ml.ingestion", INGESTION_ENV_KEYS)
        if prompt.yes(
            "Configure Databricks ingestion?",
            default=bool(ingestion.get("ML_DATABRICKS_CONNECTIONS_CONFIG")),
        ):
            _databricks(spec, files, prompt, ingestion)
        elif prompt.yes(
            "Configure File Batch ingestion?", default=bool(ingestion.get("ML_INGESTION_INPUT_DIR"))
        ):
            spec.environment["ML_INGESTION_INPUT_DIR"] = str(
                absolute_path(
                    prompt.ask(
                        "Approved input directory (outside runtime storage)",
                        ingestion.get("ML_INGESTION_INPUT_DIR", str(private / "ingestion-input")),
                        validate=absolute_path,
                    )
                )
            )
    else:
        validate_production_env(saved)
        defaults = example_environment(project)
        spec.environment = {**defaults, **{k: v for k, v in saved.items() if k in external_keys}}
        _external(spec, files, prompt, saved)
        _databricks(spec, files, prompt, saved)
    plan = build_plan(spec, files)
    print("\nPlanned private outputs (contents hidden):")
    for path in sorted(plan.outputs):
        print("  " + str(path))
    print("Runtime UID/GID:", os.geteuid(), os.getegid())
    choices = {}
    for path in plan.outputs:
        if files.originals[path] is not None:
            answer = prompt.choose(
                "Existing file " + str(path), ("overwrite", "backup", "cancel"), "cancel"
            )
            if answer == "cancel":
                raise Cancelled
            choices[path] = answer
        else:
            choices[path] = "overwrite"
    if not prompt.yes("Write these files now?"):
        raise Cancelled
    backups = files.write(plan.outputs, choices, plan.directories)
    print("Private configuration written with mode 0600. Backups:", len(backups))
    if plan.pending_sources:
        print(
            "Connection configured; no extraction source is configured. "
            "Source review/registration is pending."
        )
    if spec.connections is not None or "ML_INGESTION_INPUT_DIR" in spec.environment:
        print("Next: register a reviewed SourceDefinition before submitting ingestion.")
    command = ["docker", "compose"]
    for name in plan.env_files:
        command += ["--env-file", name]
    if profile == "compose" and len(plan.env_files) > 1:
        command += ["--profile", "ingestion"]
    for name in plan.compose_files:
        command += ["-f", name]
    print("Next, from the selected project directory:")
    print(shlex.join([*command, "config", "--quiet"]))
    print(
        "External qualification and migrations: docs/deployment/day1-deployment-runbook.md"
        if profile == "external"
        else "Follow README.md for explicit startup and account provisioning."
    )
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = SetupArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("compose", "external"))
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--private-dir", type=Path, help="Default: PROJECT/.local/ml-system")
    args = parser.parse_args(argv)
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SetupError(
                "Interactive setup requires a terminal; no credentials are accepted as arguments."
            )
        project = absolute_path(args.project_dir)
        private = absolute_path(args.private_dir or project / ".local/ml-system")
        prompt = Prompt()
        profile = args.profile or prompt.choose(
            "Deployment profile", ("compose", "external"), "compose"
        )
        configure(project, private, profile, prompt)
        return 0
    except (Cancelled, EOFError, KeyboardInterrupt):
        print("Setup cancelled.")
        return 130
    except getpass.GetPassWarning:
        print("Setup stopped: terminal cannot hide credentials.", file=sys.stderr)
        return 2
    except SetupError as error:
        print("Setup stopped: " + str(error), file=sys.stderr)
        return 2
    except (MLError, OSError, ValueError, TypeError, KeyError, RecursionError):
        print(
            "Setup stopped: configuration or file validation failed; no credentials are displayed.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
