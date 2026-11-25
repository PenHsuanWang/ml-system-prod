"""Qualify a disposable hybrid stack against its own HTTPS/auth MLflow fixture.

Requires a prebuilt pinned production-compatible runtime image and Docker Compose.
Creates only uniquely named projects and fresh synthetic private configuration.
Stops its own containers without deleting volumes. Never reads site credentials.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def private(path, value, uid):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        os.chown(path.parent, uid, uid)
    path.write_text(value)
    path.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(path, uid, uid)


def run(command, *, environment=None, timeout=180):
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Fixture operation failed; private output withheld")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--mlflow-image", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--uid", type=int, default=10001)
    args = parser.parse_args()
    if not re.fullmatch(r"[^@\s]+@sha256:[a-f0-9]{64}", args.image) or not re.fullmatch(
        r"[a-f0-9]{40}", args.code_revision
    ):
        parser.error("Actual pinned image and full source revision required")
    if args.uid <= 0 or os.geteuid() not in {0, args.uid}:
        parser.error("Use the image's nonroot UID, as that UID or root for synthetic fixtures")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o755)
    project = "ml-hybrid-" + uuid4().hex[:12]
    runtime = output / "runtime"
    runtime.mkdir(mode=0o700)
    private_root = output / "private"
    private_root.mkdir(mode=0o700)
    tracking_data = output / "tracking-data"
    tracking_data.mkdir(mode=0o700)
    if os.geteuid() == 0:
        for directory in (runtime, private_root, tracking_data):
            os.chown(directory, args.uid, args.uid)
    values = {
        k: secrets.token_hex(24)
        for k in ("postgres", "mongo", "rabbit", "redis", "writer", "reader", "jwt", "flask")
    }
    infra, roles = private_root / "infra", private_root / "roles"
    private(
        infra / "postgres.env",
        f"POSTGRES_DB=ml\nPOSTGRES_USER=ml\nPOSTGRES_PASSWORD={values['postgres']}\n",
        args.uid,
    )
    private(
        infra / "mongo.env",
        f"MONGO_INITDB_ROOT_USERNAME=root\nMONGO_INITDB_ROOT_PASSWORD={values['mongo']}\n",
        args.uid,
    )
    private(
        infra / "rabbitmq.env",
        f"RABBITMQ_DEFAULT_USER=ml\nRABBITMQ_DEFAULT_PASS={values['rabbit']}\n",
        args.uid,
    )
    private(infra / "redis.conf", f"appendonly yes\nrequirepass {values['redis']}\n", args.uid)
    # Redis runs under its image UID; the host parent remains private mode 0700.
    (infra / "redis.conf").chmod(0o444)
    connection = {
        "ML_DATABASE_URL": f"postgresql+psycopg://ml:{values['postgres']}@postgres:5432/ml",
        "ML_RABBITMQ_URL": f"amqp://ml:{values['rabbit']}@rabbitmq:5672/",
        "ML_REDIS_URL": f"redis://:{values['redis']}@redis:6379/0",
        "APP_MONGO_URI": f"mongodb://root:{values['mongo']}@mongo:27017/?authSource=admin",
        "APP_JWT_SECRET": values["jwt"],
    }
    from bootstrap.deployment_entrypoint import REQUIRED

    for role, keys in REQUIRED.items():
        config = {k: connection[k] for k in keys}
        if role in {"supervisor", "control", "operator"}:
            config.update(
                MLFLOW_TRACKING_USERNAME="fixture-writer", MLFLOW_TRACKING_PASSWORD=values["writer"]
            )
        if role in {"control", "operator", "serving"}:
            config.update(
                ML_SERVING_MLFLOW_USERNAME="fixture-reader",
                ML_SERVING_MLFLOW_PASSWORD=values["reader"],
            )
        private(roles / (role + ".json"), json.dumps(config), args.uid)
    tls = private_root / "tls"
    tls.mkdir()
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=hybrid-fixture-only",
            "-addext",
            "subjectAltName=DNS:tracking,DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(tls / "server.key"),
            "-out",
            str(tls / "ca.pem"),
        ]
    )
    for path in tls.iterdir():
        path.chmod(0o600)
        if os.geteuid() == 0:
            os.chown(path, args.uid, args.uid)
    private(
        tls / "basic_auth.ini",
        "[mlflow]\ndefault_permission = READ\ndatabase_uri = sqlite:////data/auth.db\n"
        "admin_username = fixture-writer\nadmin_password = "
        + values["writer"]
        + "\nauthorization_function = mlflow.server.auth:authenticate_request_basic_auth\n",
        args.uid,
    )
    private(tls / "identities.json", json.dumps(values), args.uid)
    setup = """import json,time,requests
v=json.load(open('/run/tls/identities.json'))
s=requests.Session();s.trust_env=False;s.verify='/run/tls/ca.pem'
u='https://tracking:8443'
for i in range(60):
 try:
  if s.get(u+'/health',timeout=2).status_code==200:break
 except requests.RequestException:pass
 time.sleep(1)
else:raise RuntimeError('tracking fixture not ready')
r=s.post(u+'/api/2.0/mlflow/experiments/search',json={'max_results':1},timeout=5)
assert r.status_code==401
try:s.get(u+'/health',verify=True,timeout=5)
except requests.exceptions.SSLError:pass
else:raise RuntimeError('untrusted fixture CA unexpectedly accepted')
r=s.post(u+'/api/2.0/mlflow/users/create',auth=('fixture-writer',v['writer']),json={'username':'fixture-reader','password':v['reader']},timeout=5)
assert r.status_code==200
print('TLS, unauthenticated denial and dedicated reader configured')
"""
    private(tls / "setup.py", setup, args.uid)
    fixture = {
        "services": {
            "tracking-backend": {
                "image": args.mlflow_image,
                "user": f"{args.uid}:{args.uid}",
                "entrypoint": ["mlflow", "server"],
                "command": [
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "5000",
                    "--backend-store-uri",
                    "sqlite:////data/mlflow.db",
                    "--artifacts-destination",
                    "/data/artifacts",
                    "--serve-artifacts",
                    "--app-name",
                    "basic-auth",
                    "--allowed-hosts",
                    "tracking,tracking:8443,localhost,127.0.0.1",
                    "--uvicorn-opts",
                    "--workers 1",
                ],
                "environment": {
                    "MLFLOW_AUTH_CONFIG_PATH": "/run/tls/basic_auth.ini",
                    "MLFLOW_FLASK_SERVER_SECRET_KEY": values["flask"],
                },
                "volumes": [str(tls) + ":/run/tls:ro", str(tracking_data) + ":/data"],
            }
        },
        "networks": {"default": {"name": project + "-tracking"}},
    }
    nginx = (
        "events {}\nhttp { server { listen 8443 ssl; server_name tracking;\n"
        "ssl_certificate /run/tls/ca.pem; ssl_certificate_key /run/tls/server.key;\n"
        "client_max_body_size 64m; location / { proxy_pass http://tracking-backend:5000;\n"
        "proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https;\n"
        "proxy_read_timeout 30s; } } }\n"
    )
    private(tls / "nginx.conf", nginx, args.uid)
    fixture["services"]["tracking"] = {
        "image": "nginx:1.28-alpine",
        "volumes": [
            str(tls) + ":/run/tls:ro",
            str(tls / "nginx.conf") + ":/etc/nginx/nginx.conf:ro",
        ],
        "depends_on": ["tracking-backend"],
    }
    fixture_path = private_root / "tracking-compose.json"
    private(fixture_path, json.dumps(fixture), args.uid)
    env_values = {}
    for line in (ROOT / ".env.ml.hybrid.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env_values[key] = value
    env_values.update(
        ML_RELEASE_IMAGE=args.image,
        ML_IMAGE_DIGEST=args.image.rsplit("@", 1)[1],
        ML_CODE_REVISION=args.code_revision,
        ML_UID=str(args.uid),
        ML_GID=str(args.uid),
        ML_RUNTIME_ROOT=str(runtime),
        ML_DEPLOYMENT_CONFIG_DIR=str(roles),
        ML_HYBRID_INFRA_CONFIG_DIR=str(infra),
        ML_OPERATOR_DEFINITIONS_DIR=str(roles),
        ML_MLFLOW_CA_FILE=str(tls / "ca.pem"),
        ML_EXTERNAL_MLFLOW_URI="https://tracking:8443",
        ML_DATABRICKS_CONNECTIONS_CONFIG="/dev/null",
        ML_DATABRICKS_SOURCES_CONFIG="/dev/null",
        ML_DATABRICKS_CLIENT_SECRET_FILE="/dev/null",
        ML_MANAGEMENT_PORT="18040",
        ML_PREDICTION_PORT="18041",
        ML_INGESTION_ENABLED="false",
        ML_DATABRICKS_ENABLED="false",
    )
    public_env = output / "hybrid.env"
    private(public_env, "\n".join(k + "=" + v for k, v in env_values.items()) + "\n", args.uid)
    bypass = ",".join(
        filter(
            None,
            [
                os.environ.get("NO_PROXY", ""),
                os.environ.get("no_proxy", ""),
                "tracking,postgres,mongo,rabbitmq,redis,localhost,127.0.0.1",
            ],
        )
    )
    network_override = {
        "services": {
            role: {"environment": {"NO_PROXY": bypass, "no_proxy": bypass}}
            for role in REQUIRED
            if role != "serving"
        },
        "networks": {"default": {"external": True, "name": project + "-tracking"}},
    }
    override_path = output / "network.json"
    override_path.write_text(json.dumps(network_override))
    base = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(public_env),
        "-f",
        str(ROOT / "compose.ml.hybrid-mlflow.yaml"),
    ]
    app = base + ["-f", str(override_path)]
    external = [
        "docker",
        "compose",
        "--project-name",
        project + "-external",
        "-f",
        str(fixture_path),
    ]
    report = {
        "scope": "isolated-external-style-mlflow-fixture",
        "production_accepted": False,
        "source_revision": args.code_revision,
        "driver_revision": run(["git", "rev-parse", "HEAD"]).strip(),
        "image": args.image,
        "fixture_image": args.mlflow_image,
        "project": project,
        "status": "FAILED",
    }
    try:
        inspection = json.loads(
            run(["docker", "image", "inspect", "--format", "{{json .}}", args.image])
        )
        model = json.loads(run(base + ["--profile", "*", "config", "--format", "json"]))
        from bootstrap.deployment_cli import preflight

        report["preflight"] = preflight(model, inspection, hybrid=True)
        report["stage"] = "tracking-fixture"
        run(external + ["up", "-d"], timeout=180)
        (output / "tls-auth.txt").write_text(
            run(
                external + ["exec", "-T", "tracking-backend", "python", "/run/tls/setup.py"],
                timeout=120,
            )
        )
        report["stage"] = "application-startup"
        run(app + ["up", "-d", "--no-build"], timeout=180)
        operator = app + ["--profile", "ops", "run", "--rm", "-T", "operator"]
        report["stage"] = "dependency-qualification"
        run(
            operator
            + [
                "python",
                "-m",
                "bootstrap.deployment_cli",
                "qualify-mlflow",
                "--experiment-name",
                "hybrid-qualification",
                "--registered-model-prefix",
                "hybrid",
                "--output",
                "/app/var/mlflow-qualification.json",
            ],
            timeout=180,
        )
        seed = (
            "from bootstrap.ml_config import MLSettings;"
            "from bootstrap.ml_wiring import build_components;"
            "from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog;"
            "s=MLSettings.from_env();c=build_components(s);"
            "SQLDatasetCatalog(c.database,s.dataset_root).seed_builtin();c.database.close()"
        )
        run(operator + ["python", "-c", seed])
        test_env = dict(
            os.environ,
            ML_TEST_MANAGEMENT_URL="http://127.0.0.1:18040",
            ML_TEST_PREDICTION_URL="http://127.0.0.1:18041",
            ML_TEST_MODEL_ENVIRONMENT="prod",
            ML_TEST_OPERATOR_COMMAND_JSON=json.dumps(operator + ["python", "manage.py"]),
        )
        report["stage"] = "governed-lifecycle"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/ml/integration/test_backend_stack.py",
                "-q",
                "--junitxml=" + str(output / "lifecycle.xml"),
            ],
            cwd=ROOT,
            env=test_env,
            stdout=(output / "lifecycle.txt").open("w"),
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
        report["lifecycle_exit_status"] = result.returncode
        if result.returncode:
            raise RuntimeError("Hybrid governed lifecycle failed; retain isolated diagnostics")
        report["status"] = "COMPOSE_QUALIFIED"
        report["dependency_qualification"] = json.loads(
            (runtime / "mlflow-qualification.json").read_text()
        )
    finally:
        log = output / "lifecycle.txt"
        if log.exists():
            sanitized = log.read_text()
            for value in values.values():
                sanitized = sanitized.replace(value, "[redacted-fixture]")
            sanitized = re.sub(r"Bearer [A-Za-z0-9_.~-]+", "Bearer [redacted]", sanitized)
            sanitized = re.sub(
                r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
                "[redacted-token]",
                sanitized,
            )
            log.write_text(sanitized)
        # Stop only resources created by this invocation. Retain volumes/evidence.
        for command in (app, external):
            subprocess.run(
                command + ["down", "--remove-orphans"],
                cwd=ROOT,
                capture_output=True,
                timeout=90,
                check=False,
            )
        (output / "qualification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired):
        raise SystemExit(
            "Hybrid fixture qualification failed; inspect retained isolated evidence"
        ) from None
