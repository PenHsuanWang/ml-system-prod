"""Render generated setup files with real Compose; never start services or print secrets."""

import json
import os
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

from bootstrap.init_config import (
    OAUTH_CONTAINER_PATH,
    SetupSpec,
    build_plan,
    example_environment,
    local_defaults,
)
from bootstrap.init_files import PrivateFiles, merge_env

ROOT = Path(__file__).resolve().parents[1]


def compose_command() -> list[str]:
    if (
        shutil.which("docker")
        and subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode
        == 0
    ):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise SystemExit("Compose v2 is required for this offline rendering gate.")


def render(
    program: list[str], directory: Path, environments: tuple[str, ...], manifests: tuple[str, ...]
) -> dict:
    arguments = [*program, "--project-name", "ml-init-render-check"]
    if "compose.ml.ingestion.yaml" in manifests:
        arguments.extend(["--profile", "ingestion"])
    for filename in environments:
        arguments.extend(["--env-file", str(directory / filename)])
    for filename in manifests:
        arguments.extend(["-f", str(directory / filename)])
    # Host environment must not override the disposable test credentials.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ML_", "MLFLOW_", "APP_", "DATABRICKS_", "OTEL_"))
    }
    result = subprocess.run(
        [*arguments, "config", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit("Compose rendering failed; private output is suppressed.")
    return json.loads(result.stdout)


def verify() -> None:
    program = compose_command()
    with tempfile.TemporaryDirectory(prefix="ml-init-compose-") as temporary:
        root = Path(temporary)
        for mode in ("local", "file", "oauth", "token", "external-oauth", "external-token"):
            project = root / (mode + " user's workspace")
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
            for name in (
                ".gitignore",
                "pyproject.toml",
                ".env.ml.production.example",
                "compose.ml.yaml",
                "compose.ml.ingestion.yaml",
                "compose.ml.databricks.yaml",
                "compose.ml.databricks-token.yaml",
                "compose.ml.external-mlflow.yaml",
            ):
                shutil.copyfile(ROOT / name, project / name)
            external = mode.startswith("external-")
            spec = SetupSpec(
                project,
                project / ".local/ml-system",
                "external" if external else "compose",
                example_environment(project) if external else local_defaults({}),
            )
            if external:
                ca = project / "ca.pem"
                ca.write_text(
                    ssl.DER_cert_to_PEM_cert(
                        ssl.create_default_context().get_ca_certs(binary_form=True)[0]
                    )
                )
                spec.environment.update(
                    ML_RELEASE_IMAGE="registry.example/ml@sha256:" + "a" * 64,
                    ML_IMAGE_DIGEST="sha256:" + "a" * 64,
                    ML_CODE_REVISION="b" * 40,
                    ML_UID=str(os.geteuid()),
                    ML_GID=str(os.getegid()),
                    ML_RUNTIME_ROOT=str(project / "var"),
                    ML_MLFLOW_CA_FILE=str(ca),
                    ML_OPERATOR_DEFINITIONS_DIR=str(spec.private / "operator-definitions"),
                    ML_EXTERNAL_MLFLOW_URI="https://tracking.example",
                )
                spec.private_values = {
                    "ML_DATABASE_URL": "postgresql+psycopg://ml:dummy@postgres:5432/ml",
                    "ML_RABBITMQ_URL": "amqp://ml:dummy@rabbitmq:5672/",
                    "ML_REDIS_URL": "redis://:dummy@redis:6379/0",
                    "APP_MONGO_URI": "mongodb://ml:dummy@mongo:27017/?authSource=admin",
                    "APP_JWT_SECRET": "j" * 64,
                    "MLFLOW_TRACKING_TOKEN": "writer-test-only",
                    "ML_SERVING_MLFLOW_TOKEN": "reader-test-only",
                }
            elif mode != "local":
                spec.environment["ML_INGESTION_INPUT_DIR"] = str(spec.private / "input")
            if mode not in {"local", "file"}:
                connection = {
                    "server_hostname": "dbc-approved.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/0123abcd",
                }
                if mode.endswith("token"):
                    connection["access_token_env"] = "DATABRICKS_TOKEN"
                    spec.environment["DATABRICKS_TOKEN"] = "test$TOKEN#literal"
                else:
                    connection.update(
                        client_id="principal", client_secret_file=OAUTH_CONTAINER_PATH
                    )
                    spec.oauth_secret = "test-oauth-only"
                spec.connections = {"warehouse-main": connection}
            files = PrivateFiles(project)
            plan = build_plan(spec, files)
            files.write(plan.outputs, dict.fromkeys(plan.outputs, "overwrite"), plan.directories)
            model = render(program, project, plan.env_files, plan.compose_files)
            assert model["services"]["management"]["environment"]["ML_MODEL_ENVIRONMENT"] == (
                "prod" if external else "dev"
            )
            if mode in {"file", "oauth", "token", "external-oauth", "external-token"}:
                assert "ingestion-worker" in model["services"]
            if external:
                assert not {"mlflow", "minio", "postgres", "rabbitmq", "redis"} & set(
                    model["services"]
                )
                assert model["services"]["control"]["secrets"]
            print("Generated Compose profile verified:", mode)
        literal = root / "literal"
        literal.mkdir()
        (literal / "compose.yaml").write_text(
            "services:\n  probe:\n    image: busybox\n    environment:\n"
            "      ML_INIT_LITERAL: ${ML_INIT_LITERAL}\n"
        )
        values = [
            "a$TOKEN#hash",
            "a'b",
            "a\\b",
            "double\\\\slash",
            "\\'quote",
            "tail\\",
            "$tail\\",
            "$$",
            "space path",
        ]
        for value in values:
            (literal / ".env").write_text(merge_env("", {"ML_INIT_LITERAL": value}))
            model = render(program, literal, (".env",), ("compose.yaml",))
            # Compose escapes dollars in its reusable config representation.
            actual = model["services"]["probe"]["environment"]["ML_INIT_LITERAL"].replace("$$", "$")
            assert actual == value, "Compose changed a literal test value"
        print("Compose literal environment values verified:", len(values))


if __name__ == "__main__":
    verify()
