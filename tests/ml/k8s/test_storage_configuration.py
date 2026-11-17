"""Storage mount and qualification declarations; no ML dependencies or cluster access."""

from types import SimpleNamespace

import pytest
import yaml

from scripts.k8s_foundation import ROOT
from scripts.verify_k8s_storage import declarations, run


def test_cache_is_disposable_and_durable_absolute_paths_are_preserved():
    documents = list(yaml.safe_load_all((ROOT / "deploy/k8s/base/workloads.yaml").read_text()))
    for document in documents:
        pod = document["spec"]["template"]["spec"]
        volumes = {value["name"]: value for value in pod["volumes"]}
        assert volumes["runtime"]["persistentVolumeClaim"]["claimName"] == "ml-runtime"
        assert volumes["artifact-cache"]["emptyDir"] == {"sizeLimit": "1Gi"}
        for container in pod["containers"]:
            mounts = {value["name"]: value for value in container["volumeMounts"]}
            assert mounts["runtime"]["mountPath"] == "/app/var"
            assert mounts["artifact-cache"]["mountPath"] == "/app/var/artifacts"
            assert "ephemeral-storage" in container["resources"]["limits"]


def qualification():
    return list(
        yaml.safe_load_all((ROOT / "deploy/k8s/storage-contracts/resources.yaml").read_text())
    )


def test_qualification_uses_a_new_claim_and_readonly_final_reader():
    documents = qualification()
    claim = next(value for value in documents if value["kind"] == "PersistentVolumeClaim")
    assert claim["metadata"]["name"] == "storage-contracts"
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    pods = [value for value in documents if value["kind"] == "Pod"]
    assert {pod["metadata"]["name"] for pod in pods} == {
        "storage-writer",
        "storage-recovery",
        "storage-reader",
    }
    for pod in pods:
        spec = pod["spec"]
        assert spec["automountServiceAccountToken"] is False
        assert spec["securityContext"]["runAsUser"] == 10001
        assert spec["restartPolicy"] == "Never"
        mounts = spec["containers"][0]["volumeMounts"]
        assert mounts[0]["readOnly"] == (pod["metadata"]["name"] == "storage-reader")
        assert mounts[1]["mountPath"] == "/app/var/artifacts" and not mounts[1].get("readOnly")
        assert all("secret" not in volume for volume in spec["volumes"])


def test_csi_settings_and_reader_placement_are_explicit(monkeypatch):
    documents = qualification()
    monkeypatch.setattr(
        "scripts.verify_k8s_storage.command",
        lambda *_: SimpleNamespace(stdout=yaml.safe_dump_all(documents)),
    )
    result = declarations(
        "registry.test/storage@sha256:" + "a" * 64, "approved-csi", "ReadWriteMany", "node-b"
    )
    claim = next(value for value in result if value["kind"] == "PersistentVolumeClaim")
    assert claim["spec"]["storageClassName"] == "approved-csi"
    assert claim["spec"]["accessModes"] == ["ReadWriteMany"]
    reader = next(value for value in result if value["metadata"]["name"] == "storage-reader")
    assert reader["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "node-b"}
    with pytest.raises(ValueError):
        declarations("storage:latest")


def test_existing_qualification_namespace_is_never_reused(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.verify_k8s_storage.ROOT", tmp_path)
    monkeypatch.setattr("scripts.verify_k8s_storage.declarations", lambda *a: [])
    monkeypatch.setattr(
        "scripts.verify_k8s_storage.command",
        lambda *a: SimpleNamespace(stdout="namespace/already-exists"),
    )
    args = SimpleNamespace(
        output=tmp_path / "var/evidence",
        image="unused",
        storage_class=None,
        access_mode="ReadWriteOnce",
        reader_node=None,
        context="minikube",
    )
    with pytest.raises(ValueError, match="already exists"):
        run(args)
