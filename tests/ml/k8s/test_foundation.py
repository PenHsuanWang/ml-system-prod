"""Foundation boundary tests; lifecycle and CSI qualification belong to later PRs."""

import importlib.util
import json
import stat
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from scripts.k8s_foundation import IMAGES, NAMESPACE, ROOT, deploy, image_overrides, prepare


def load_copy():
    spec = importlib.util.spec_from_file_location(
        "foundation_copy", ROOT / "deploy/k8s/base/files/prepare_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.copy_private


def test_projected_symlink_becomes_private_regular_owned_file(tmp_path):
    value = tmp_path / "projected-value"
    value.write_text('{"private":"value"}')
    source = tmp_path / "projected"
    source.symlink_to(value)
    destination = tmp_path / "private/runtime.json"
    load_copy()(source, destination)
    assert not destination.is_symlink()
    assert destination.read_bytes() == value.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert destination.stat().st_uid == value.stat().st_uid
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    load_copy()(source, destination)


def test_copy_refuses_existing_destination_and_symlink_parent(tmp_path):
    source = tmp_path / "source"
    source.write_text("value")
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        load_copy()(source, alias / "runtime.json")
    destination = target / "runtime.json"
    destination.write_text("existing")
    with pytest.raises(FileExistsError):
        load_copy()(source, destination)
    assert destination.read_text() == "existing"


@pytest.mark.parametrize("data", [b"", b"x" * 65537])
def test_copy_rejects_invalid_size_before_creating_destination(tmp_path, data):
    source = tmp_path / "source"
    source.write_bytes(data)
    with pytest.raises(ValueError):
        load_copy()(source, tmp_path / "private/runtime.json")
    assert not (tmp_path / "private/runtime.json").exists()


@pytest.mark.parametrize(
    "reference", ["image:latest", "image@sha256:" + "0" * 64, "image@sha256:bad"]
)
def test_release_rejects_unpinned_or_placeholder_images(reference):
    images = {name: "example.test/ml@sha256:" + "a" * 64 for name in IMAGES}
    images[IMAGES[0]] = reference
    with pytest.raises(ValueError):
        image_overrides(images)


def test_private_overlay_uses_role_allowlists_and_never_reinitializes(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.k8s_foundation.ROOT", tmp_path)
    directory = tmp_path / "var/foundation"
    images = {name: "example.test/ml@sha256:" + "a" * 64 for name in IMAGES}
    prepare(directory, "b" * 40, images)
    expected = {
        "migrate": {"ML_DATABASE_URL"},
        "management": {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"},
        "relay": {"ML_DATABASE_URL", "ML_RABBITMQ_URL"},
        "supervisor": {
            "ML_DATABASE_URL",
            "ML_RABBITMQ_URL",
            "ML_REDIS_URL",
            "MLFLOW_TRACKING_USERNAME",
            "MLFLOW_TRACKING_PASSWORD",
        },
        "control": {
            "ML_DATABASE_URL",
            "APP_MONGO_URI",
            "APP_JWT_SECRET",
            "MLFLOW_TRACKING_USERNAME",
            "MLFLOW_TRACKING_PASSWORD",
            "ML_SERVING_MLFLOW_USERNAME",
            "ML_SERVING_MLFLOW_PASSWORD",
        },
        "gateway": {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"},
        "ingestion-worker": {"ML_DATABASE_URL"},
    }
    for role, keys in expected.items():
        path = directory / (role + ".json")
        assert set(json.loads(path.read_text())) == keys
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    overlay = yaml.safe_load((directory / "kustomization.yaml").read_text())
    assert len(overlay["secretGenerator"]) == 11
    assert "MLFLOW_TRACKING_PASSWORD" not in (directory / "kustomization.yaml").read_text()
    assert (directory / "ca.crt").exists() and (directory / "tls.key").exists()
    with pytest.raises(FileExistsError):
        prepare(directory, "b" * 40, images)


def test_private_overlay_refuses_output_outside_runtime_tree(tmp_path):
    images = {name: "example.test/ml@sha256:" + "a" * 64 for name in IMAGES}
    with pytest.raises(ValueError):
        prepare(tmp_path / "outside", "b" * 40, images)


@pytest.mark.parametrize("migration_fails", [True, False])
def test_application_apply_requires_completed_migration(tmp_path, monkeypatch, migration_fails):
    documents = []
    for directory in (ROOT / "deploy/k8s/base", ROOT / "deploy/k8s/infrastructure/minikube"):
        for path in directory.glob("*.yaml"):
            if path.name == "kustomization.yaml":
                continue
            documents.extend(yaml.safe_load_all(path.read_text()))
    identities = {(document["kind"], document["metadata"]["name"]) for document in documents}
    assert {("Job", "ml-migrate"), ("Deployment", "ml-control")} <= identities, (
        "The acceptance image must contain the Kubernetes manifest fixtures"
    )
    for document in documents:
        if document["kind"] != "Namespace":
            document["metadata"]["namespace"] = NAMESPACE
        if document["kind"] in {"Deployment", "Job"}:
            pod = document["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                container["image"] = "example.test/image@sha256:" + "a" * 64
    calls = []

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:2] == ["kubectl", "kustomize"]:
            return SimpleNamespace(stdout=yaml.safe_dump_all(documents))
        if "job/ml-migrate" in arguments and migration_fails:
            raise subprocess.CalledProcessError(1, arguments)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("scripts.k8s_foundation.run", execute)
    monkeypatch.setattr(
        "scripts.k8s_foundation.subprocess.run", lambda *a, **kw: SimpleNamespace(stdout="")
    )
    if migration_fails:
        with pytest.raises(subprocess.CalledProcessError):
            deploy(tmp_path)
        assert not (tmp_path / "runtime.yaml").exists()
    else:
        deploy(tmp_path)
        migration_wait = next(i for i, args in enumerate(calls) if "job/ml-migrate" in args)
        runtime_apply = next(
            i for i, args in enumerate(calls) if str(tmp_path / "runtime.yaml") in args
        )
        assert migration_wait < runtime_apply
    assert all(
        "--context=minikube" in args for args in calls if args[:2] != ["kubectl", "kustomize"]
    )


def test_existing_namespace_refuses_reinitialization(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.k8s_foundation.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout="namespace/" + NAMESPACE),
    )
    with pytest.raises(ValueError, match="already exists"):
        deploy(tmp_path)
    assert not list(tmp_path.iterdir())
