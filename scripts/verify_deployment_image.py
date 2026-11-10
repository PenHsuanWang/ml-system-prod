"""Smoke a locally built unified image; this does not approve a Registry release or site."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.STDOUT).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inspection = json.loads(docker("image", "inspect", args.image, "--format", "{{json .}}"))
    if (
        inspection["Config"]["Labels"]["org.opencontainers.image.revision"] != args.code_revision
        or inspection["Config"]["Entrypoint"] != ["python", "-m", "bootstrap.deployment_entrypoint"]
        or inspection["Os"] != "linux"
        or inspection["Architecture"] != "amd64"
    ):
        raise SystemExit("Unified image metadata does not match this build")
    (args.output_dir / "image-inspect.json").write_text(json.dumps(inspection, indent=2) + "\n")
    packages = docker(
        "run",
        "--rm",
        "--entrypoint",
        "python",
        args.image,
        "-c",
        "import sys,torch,scipy; from importlib.metadata import version; "
        "from databricks import sql; "
        "from databricks.sdk.core import Config,oauth_service_principal; "
        "import bootstrap.deployment_cli,bootstrap.deployment_entrypoint; "
        'assert sys.version_info[:2]==(3,12); assert torch.__version__.split("+")[0]=="2.6.0"; '
        'print({"python":sys.version.split()[0],"torch":torch.__version__, '
        '"scipy":scipy.__version__,"connector":version("databricks-sql-connector")})',
    )
    if os.getuid() == 0:
        raise SystemExit("Run image entrypoint qualification as a non-root operator")
    with tempfile.TemporaryDirectory(prefix="deployment-image-smoke-") as temporary:
        root = Path(temporary)
        configuration = root / "migrate.json"
        configuration.write_text(
            json.dumps(
                {
                    "ML_DATABASE_URL": "postgresql+psycopg://test:fixture-only@not-connected.invalid/test",
                }
            )
        )
        configuration.chmod(0o600)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        output = docker(
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={configuration},dst=/run/secrets/runtime.json,readonly",
            "--mount",
            f"type=bind,src={runtime},dst=/app/var",
            "--mount",
            "type=bind,src=/etc/ssl/certs/ca-certificates.crt,"
            "dst=/run/config/mlflow-ca.pem,readonly",
            "-e",
            "ML_RUNTIME_ROLE=migrate",
            "-e",
            "ML_RUNTIME_CONFIG_FILE=/run/secrets/runtime.json",
            "-e",
            "ML_MODEL_ENVIRONMENT=prod",
            "-e",
            "ML_TRACKER_BACKEND=mlflow",
            "-e",
            "ML_TRACKING_URI=https://not-connected.invalid",
            "-e",
            "MLFLOW_TRACKING_URI=https://not-connected.invalid",
            "-e",
            "MLFLOW_TRACKING_INSECURE_TLS=false",
            "-e",
            "MLFLOW_TRACKING_SERVER_CERT_PATH=/run/config/mlflow-ca.pem",
            "-e",
            "ML_EXECUTION_ROOT=/app/var/executions",
            "-e",
            "ML_IMAGE_DIGEST=" + inspection["Id"],
            "-e",
            "ML_CODE_REVISION=" + args.code_revision,
            args.image,
            "python",
            "-c",
            "import os,stat; from pathlib import Path; "
            'assert os.environ["ML_DEPLOYMENT_ROLE"]=="management"; '
            'assert os.environ["ML_DATABASE_URL"].startswith("postgresql+"); '
            "assert stat.S_IMODE(Path.home().stat().st_mode)==0o700; "
            'print("PRIVATE_CONFIGURATION_ENTRYPOINT_PASSED")',
        )
    report = {
        "status": "UNIFIED_IMAGE_SMOKE_PASSED",
        "packages": packages,
        "entrypoint": output,
        "image_config_id": inspection["Id"],
        "code_revision": args.code_revision,
        "registry_release_qualified": False,
        "external_dependencies_qualified": False,
        "production_accepted": False,
    }
    (args.output_dir / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
