"""Prepare and start an isolated Minikube foundation; never select a production context."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "ml-k8s-foundation"
ROLES = ("migrate", "management", "relay", "supervisor", "control", "gateway", "ingestion-worker")
IMAGES = (
    "ml-system-runtime:unconfigured",
    "ml-system-mlflow:unconfigured",
    "ml-system-minio:unconfigured",
    "ml-system-mc:unconfigured",
    "postgres:17",
    "rabbitmq:4.4-management",
    "redis:7",
    "mongo:7",
    "nginx:1.28-alpine",
)


def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, check=True, timeout=600, **kwargs)


def private_write(path: Path, data: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(data)


def image_overrides(images: dict[str, str]) -> list[dict[str, str]]:
    if set(images) != set(IMAGES):
        raise ValueError("Image lock must contain exactly the documented image names")
    overrides = []
    for name, reference in images.items():
        if not re.fullmatch(r"[a-zA-Z0-9._/:-]+@sha256:[a-f0-9]{64}", reference):
            raise ValueError("Every image must use a real repository manifest digest")
        repository, digest = reference.split("@")
        if digest == "sha256:" + "0" * 64:
            raise ValueError("Placeholder image digests are forbidden")
        overrides.append({"name": name, "newName": repository, "digest": digest})
    return overrides


def lock_images(directory: Path, local_images: dict[str, str]) -> None:
    listing = run(
        [
            "minikube",
            "-p",
            "minikube",
            "ssh",
            "--",
            "sudo",
            "ctr",
            "-n",
            "k8s.io",
            "images",
            "list",
        ],
        capture_output=True,
        text=True,
    ).stdout
    descriptors = {
        parts[0]: parts[2] for line in listing.splitlines()[1:] if len(parts := line.split()) >= 3
    }
    images = {}
    for name in IMAGES:
        source = local_images.get(name, name)
        if "/" not in source:
            source = "docker.io/library/" + source
        digest = descriptors.get(source)
        if not digest or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Load all documented images into the minikube profile first")
        reference = source.rsplit(":", 1)[0] + "@" + digest
        if reference not in descriptors:
            # Docker archive imports may have no repoDigest alias. Register the
            # exact existing manifest so kubelet can find the pinned local image.
            run(
                [
                    "minikube",
                    "-p",
                    "minikube",
                    "ssh",
                    "--",
                    "sudo",
                    "ctr",
                    "-n",
                    "k8s.io",
                    "images",
                    "tag",
                    source,
                    reference,
                ],
                stdout=subprocess.DEVNULL,
            )
        images[name] = reference
    image_overrides(images)
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(directory, json.dumps(images, indent=2) + "\n")
    print("Containerd manifest image lock recorded")


def prepare(directory: Path, revision: str, images: dict[str, str]) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or revision == "0" * 40:
        raise ValueError("A complete release Git commit is required")
    overrides = image_overrides(images)
    # Secret output must be in the repository's already-ignored runtime tree.
    directory = directory.absolute()
    approved = ROOT / "var"
    if approved not in directory.parents or any(
        p.is_symlink() for p in (directory, *directory.parents)
    ):
        raise ValueError("Private output must be a nonsymlink directory below repository var/")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    values = {
        key: secrets.token_hex(24)
        for key in (
            "postgres",
            "rabbit",
            "mongo",
            "redis",
            "minio",
            "writer",
            "reader",
            "jwt",
            "flask",
        )
    }
    database = f"postgresql+psycopg://ml:{values['postgres']}@postgres:5432/ml"
    connection = {
        "ML_DATABASE_URL": database,
        "ML_RABBITMQ_URL": f"amqp://ml:{values['rabbit']}@rabbitmq:5672/",
        "ML_REDIS_URL": f"redis://:{values['redis']}@redis:6379/0",
        "APP_MONGO_URI": f"mongodb://ml:{values['mongo']}@mongodb:27017/backend?authSource=admin",
        "APP_JWT_SECRET": values["jwt"],
    }
    fields = {
        "migrate": ["ML_DATABASE_URL"],
        "management": ["ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"],
        "relay": ["ML_DATABASE_URL", "ML_RABBITMQ_URL"],
        "supervisor": ["ML_DATABASE_URL", "ML_RABBITMQ_URL", "ML_REDIS_URL"],
        "control": ["ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"],
        "gateway": ["ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"],
        "ingestion-worker": ["ML_DATABASE_URL"],
    }
    generators = []
    for role in ROLES:
        config = {key: connection[key] for key in fields[role]}
        if role in {"supervisor", "control"}:
            config.update(
                MLFLOW_TRACKING_USERNAME="foundation-writer",
                MLFLOW_TRACKING_PASSWORD=values["writer"],
            )
        if role == "control":
            config.update(
                ML_SERVING_MLFLOW_USERNAME="foundation-reader",
                ML_SERVING_MLFLOW_PASSWORD=values["reader"],
            )
        private_write(directory / (role + ".json"), json.dumps(config))
        generators.append(
            {"name": "ml-runtime-" + role, "files": ["runtime.json=" + role + ".json"]}
        )
    private_write(
        directory / "infra.env",
        "\n".join(
            f"{key}={value}"
            for key, value in {
                "POSTGRES_PASSWORD": values["postgres"],
                "RABBITMQ_DEFAULT_PASS": values["rabbit"],
                "MONGO_INITDB_ROOT_PASSWORD": values["mongo"],
                "MINIO_ROOT_PASSWORD": values["minio"],
                "MLFLOW_BACKEND_STORE_URI": f"postgresql+psycopg://ml:{values['postgres']}@postgres:5432/mlflow",
                "AWS_ACCESS_KEY_ID": "ml-artifacts",
                "AWS_SECRET_ACCESS_KEY": values["minio"],
                "MLFLOW_FLASK_SERVER_SECRET_KEY": values["flask"],
            }.items()
        )
        + "\n",
    )
    private_write(directory / "redis.conf", f"appendonly yes\nrequirepass {values['redis']}\n")
    private_write(
        directory / "basic_auth.ini",
        "[mlflow]\n"
        "default_permission = READ\ndatabase_uri = sqlite:////auth-data/basic_auth.db\n"
        f"admin_username = foundation-writer\nadmin_password = {values['writer']}\n"
        "authorization_function = mlflow.server.auth:authenticate_request_basic_auth\n",
    )
    generators += [
        {"name": "ml-infra", "envs": ["infra.env"]},
        {"name": "ml-redis", "files": ["redis.conf"]},
        {"name": "mlflow-auth", "files": ["basic_auth.ini"]},
        {"name": "mlflow-tls", "type": "kubernetes.io/tls", "files": ["tls.crt", "tls.key"]},
    ]

    def openssl(*arguments: str) -> None:
        run(
            ["openssl", *arguments],
            cwd=directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "30",
        "-subj",
        "/CN=Minikube Foundation CA",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
    )
    openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=mlflow",
        "-keyout",
        "tls.key",
        "-out",
        "tls.csr",
    )
    private_write(
        directory / "tls.ext",
        f"subjectAltName=DNS:mlflow,DNS:mlflow.{NAMESPACE}.svc,DNS:mlflow.{NAMESPACE}.svc.cluster.local\nextendedKeyUsage=serverAuth\n",
    )
    openssl(
        "x509",
        "-req",
        "-in",
        "tls.csr",
        "-CA",
        "ca.crt",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-days",
        "30",
        "-extfile",
        "tls.ext",
        "-out",
        "tls.crt",
    )
    config = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": NAMESPACE,
        "resources": [os.path.relpath(ROOT / "deploy/k8s/overlays/minikube", directory)],
        "images": overrides,
        "generatorOptions": {"disableNameSuffixHash": True},
        "secretGenerator": generators,
        "configMapGenerator": [
            {"name": "mlflow-ca", "files": ["ca.crt"]},
            {
                "name": "ml-runtime",
                "behavior": "merge",
                "literals": [
                    "ML_CODE_REVISION=" + revision,
                    "ML_IMAGE_DIGEST=" + images["ml-system-runtime:unconfigured"].split("@")[1],
                ],
            },
        ],
    }
    private_write(directory / "kustomization.yaml", yaml.safe_dump(config, sort_keys=False))
    private_write(directory / "image-lock.json", json.dumps(images, indent=2) + "\n")
    print("Private Minikube overlay prepared (credentials omitted)")


def deploy(directory: Path) -> None:
    kubectl = ["kubectl", "--context=minikube", "--namespace=" + NAMESPACE]
    # Refuse to overlay an existing environment: initialization is a fresh-install
    # workflow, not an upgrade/secret-rotation or database recovery mechanism.
    existing = subprocess.run(
        [*kubectl, "get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    if existing.stdout.strip():
        raise ValueError(
            "Foundation namespace already exists; follow the runbook, do not reinitialize"
        )
    rendered = run(["kubectl", "kustomize", str(directory)], capture_output=True, text=True).stdout
    resources = list(yaml.safe_load_all(rendered))
    for document in resources:
        if document["kind"] == "Namespace" and document["metadata"]["name"] != NAMESPACE:
            raise ValueError("Rendered namespace escaped the foundation boundary")
        if (
            document.get("kind") != "Namespace"
            and document["metadata"].get("namespace") != NAMESPACE
        ):
            raise ValueError("Rendered resource escaped the foundation namespace")
        if document["kind"] in {"Deployment", "Job"}:
            pod = document["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                if not re.fullmatch(r"[a-zA-Z0-9._/:-]+@sha256:[a-f0-9]{64}", container["image"]):
                    raise ValueError("Rendered workload image is not digest-pinned")

    def apply(name: str, documents: list[dict]) -> None:
        path = directory / (name + ".yaml")
        private_write(path, yaml.safe_dump_all(documents, sort_keys=False))
        # Never echo rendered Secret data into logs or include it in evidence.
        run(
            [*kubectl, "apply", "--server-side", "--field-manager=ml-foundation", "-f", str(path)],
            stdout=subprocess.DEVNULL,
        )

    common, infrastructure, migration, runtime = [], [], [], []
    for document in resources:
        if document["kind"] not in {"Deployment", "Job"}:
            common.append(document)
        elif document["metadata"]["name"] == "ml-migrate":
            migration.append(document)
        elif (
            document["spec"]["template"]["metadata"]["labels"].get("ml-system/stage")
            == "infrastructure"
        ):
            infrastructure.append(document)
        else:
            runtime.append(document)
    apply("common", common)
    apply("infrastructure", infrastructure)
    for name in ("postgres", "rabbitmq", "redis", "mongodb", "minio", "mlflow"):
        run([*kubectl, "rollout", "status", "deployment/" + name, "--timeout=300s"])
    run([*kubectl, "wait", "--for=condition=complete", "job/minio-init", "--timeout=180s"])
    apply("migration", migration)
    run([*kubectl, "wait", "--for=condition=complete", "job/ml-migrate", "--timeout=300s"])
    apply("runtime", runtime)
    for name in ("management", "relay", "supervisor", "control", "ingestion-worker"):
        run([*kubectl, "rollout", "status", "deployment/ml-" + name, "--timeout=300s"])
    print("FOUNDATION_STARTED: initialization only; recovery and production remain unqualified")


def verify(directory: Path) -> None:
    if (directory / "smoke.json").exists():
        raise FileExistsError("Retain the previous smoke receipt before another attempt")
    pods = json.loads(
        run(
            [
                "kubectl",
                "--context=minikube",
                "--namespace=" + NAMESPACE,
                "get",
                "pods",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
        ).stdout
    )
    validated, ready = set(), set()
    for pod in pods["items"]:
        if not pod["metadata"]["name"].startswith("ml-"):
            continue
        for status in pod["status"].get("initContainerStatuses", []):
            if status["name"].startswith("prepare-"):
                if status.get("state", {}).get("terminated", {}).get("exitCode") != 0:
                    raise ValueError("A role did not pass production bootstrap initialization")
                validated.add(status["name"].removeprefix("prepare-"))
        for status in pod["status"].get("containerStatuses", []):
            if status["name"] in ROLES and status["name"] != "migrate":
                if not status.get("ready") or "running" not in status.get("state", {}):
                    raise ValueError("A runtime role is not ready")
                ready.add(status["name"])
    if validated != set(ROLES) or ready != set(ROLES) - {"migrate"}:
        raise ValueError("Foundation role coverage is incomplete")
    report = run(
        [
            "kubectl",
            "--context=minikube",
            "--namespace=" + NAMESPACE,
            "exec",
            "-i",
            "deployment/ml-control",
            "-c",
            "control",
            "--",
            "python",
            "-",
        ],
        input=(ROOT / "scripts/k8s_mlflow_smoke.py").read_text(),
        capture_output=True,
        text=True,
    ).stdout
    value = json.loads(report)
    if value.get("status") != "FOUNDATION_SMOKE_PASSED":
        raise ValueError("Foundation smoke did not pass")
    value.update(bootstrap_roles_validated=sorted(validated), runtime_roles_ready=sorted(ready))
    private_write(directory / "smoke.json", json.dumps(value, indent=2) + "\n")
    print(json.dumps(value))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    lock_parser = commands.add_parser("lock-images")
    lock_parser.add_argument("--runtime-image", required=True)
    lock_parser.add_argument("--mlflow-image", required=True)
    lock_parser.add_argument("--minio-image", required=True)
    lock_parser.add_argument("--mc-image", required=True)
    lock_parser.add_argument("--output", type=Path, default=ROOT / "var/k8s-image-lock.json")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--image-lock", type=Path, required=True)
    prepare_parser.add_argument("--revision", required=True)
    prepare_parser.add_argument("--directory", type=Path, default=ROOT / "var/k8s-foundation")
    deploy_parser = commands.add_parser("deploy")
    deploy_parser.add_argument("--directory", type=Path, default=ROOT / "var/k8s-foundation")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--directory", type=Path, default=ROOT / "var/k8s-foundation")
    args = parser.parse_args()
    if args.operation == "lock-images":
        lock_images(
            args.output,
            dict(
                zip(
                    IMAGES[:4],
                    (args.runtime_image, args.mlflow_image, args.minio_image, args.mc_image),
                    strict=True,
                )
            ),
        )
    elif args.operation == "prepare":
        prepare(args.directory, args.revision, json.loads(args.image_lock.read_text()))
    elif args.operation == "verify":
        verify(args.directory.absolute())
    else:
        deploy(args.directory.absolute())


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit(
            "Foundation operation failed; inspect private configuration and cluster state"
        ) from None
