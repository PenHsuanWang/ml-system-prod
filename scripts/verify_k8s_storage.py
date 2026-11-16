"""Run synthetic storage contracts on a new PVC using distinct writer/recovery/reader Pods."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "ml-k8s-storage-contracts"


def command(arguments: list[str], **kwargs):
    return subprocess.run(
        arguments, check=True, timeout=90, capture_output=True, text=True, **kwargs
    )


def record(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)


def lock_image(tag: str, output: Path) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*:[A-Za-z0-9_.-]+", tag):
        raise ValueError("Use a local, explicitly tagged image without shell metacharacters")
    source = "docker.io/library/" + tag
    prefix = ["minikube", "-p", "minikube", "ssh", "--", "sudo", "ctr", "-n", "k8s.io", "images"]
    rows = [line.split() for line in command([*prefix, "list"]).stdout.splitlines()[1:]]
    matches = [parts[2] for parts in rows if len(parts) >= 3 and parts[0] == source]
    if len(matches) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", matches[0]):
        raise ValueError("Load the qualification image into the minikube profile first")
    reference = source.rsplit(":", 1)[0] + "@" + matches[0]
    if not any(parts[0] == reference for parts in rows):
        command([*prefix, "tag", source, reference])
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record(output, reference + "\n")
    print("Qualification manifest digest recorded")


def declarations(image: str, storage_class=None, access_mode="ReadWriteOnce", reader_node=None):
    if not re.fullmatch(r"[a-zA-Z0-9._/:-]+@sha256:[a-f0-9]{64}", image) or image.endswith(
        "0" * 64
    ):
        raise ValueError("Qualification requires an immutable manifest image reference")
    rendered = command(["kubectl", "kustomize", str(ROOT / "deploy/k8s/storage-contracts")]).stdout
    documents = list(yaml.safe_load_all(rendered))
    for document in documents:
        if document["kind"] == "PersistentVolumeClaim":
            document["spec"]["accessModes"] = [access_mode]
            if storage_class is not None:
                document["spec"]["storageClassName"] = storage_class
        if document["kind"] == "Pod":
            document["spec"]["containers"][0]["image"] = image
            if document["metadata"]["name"] == "storage-reader" and reader_node is not None:
                document["spec"]["nodeSelector"] = {"kubernetes.io/hostname": reader_node}
    return documents


def run(args) -> None:
    directory = args.output.absolute()
    if ROOT / "var" not in directory.parents or any(
        path.is_symlink() for path in (directory, *directory.parents)
    ):
        raise ValueError("Evidence must be a new nonsymlink directory below repository var/")
    documents = declarations(args.image, args.storage_class, args.access_mode, args.reader_node)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    kubectl = ["kubectl", "--context=" + args.context, "--namespace=" + NAMESPACE]

    def kub(*arguments):
        return command([*kubectl, *arguments])

    if kub("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name").stdout.strip():
        raise ValueError("Qualification namespace already exists; preserve previous evidence")
    for doc in documents:
        if doc["kind"] == "Namespace":
            if doc["metadata"]["name"] != NAMESPACE:
                raise ValueError("Unexpected qualification namespace")
        elif doc["metadata"].get("namespace") != NAMESPACE:
            raise ValueError("Unexpected qualification resource scope")
    record(directory / "rendered.yaml", yaml.safe_dump_all(documents, sort_keys=False))

    def apply(name, selected):
        path = directory / (name + ".yaml")
        record(path, yaml.safe_dump_all(selected, sort_keys=False))
        kub("apply", "--server-side", "--field-manager=ml-storage-contracts", "-f", str(path))

    def observe(name, marker=None):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            pod = json.loads(kub("get", "pod", name, "-o", "json").stdout)
            if pod["status"]["phase"] == "Failed":
                record(directory / (name + ".log"), kub("logs", name).stdout)
                raise RuntimeError("Qualification Pod failed: " + name)
            if marker is not None:
                try:
                    logs = kub("logs", name).stdout
                except subprocess.CalledProcessError:
                    logs = ""
                if marker in logs.splitlines():
                    return pod
            elif pod["status"]["phase"] == "Succeeded":
                return pod
            time.sleep(2)
        raise RuntimeError("Qualification Pod exceeded its deadline: " + name)

    apply("common", [d for d in documents if d["kind"] != "Pod"])
    # A WaitForFirstConsumer PVC binds only after the first Pod is scheduled.
    apply(
        "writer",
        [d for d in documents if d["kind"] == "Pod" and d["metadata"]["name"] == "storage-writer"],
    )
    writer = observe("storage-writer", "POD_CRASH_READY")
    print("Writer reached the post-rename crash boundary", flush=True)
    pvc = json.loads(kub("get", "pvc", "storage-contracts", "-o", "json").stdout)
    pv = json.loads(kub("get", "pv", pvc["spec"]["volumeName"], "-o", "json").stdout)
    # Normal API deletion waits for kubelet confirmation; never force-finalize.
    kub("delete", "pod", "storage-writer", "--grace-period=1", "--wait=true", "--timeout=60s")
    apply(
        "recovery",
        [
            d
            for d in documents
            if d["kind"] == "Pod" and d["metadata"]["name"] == "storage-recovery"
        ],
    )
    recovery = observe("storage-recovery")
    print("New Pod reconciled the same publication intent", flush=True)
    kub("delete", "pod", "storage-recovery", "--wait=true", "--timeout=60s")
    apply(
        "reader",
        [d for d in documents if d["kind"] == "Pod" and d["metadata"]["name"] == "storage-reader"],
    )
    reader = observe("storage-reader")
    logs = kub("logs", "storage-reader").stdout
    record(directory / "reader.log", logs)
    result = json.loads(logs.strip().splitlines()[-1])
    if result.get("status") != "STORAGE_CONTRACTS_PASSED":
        raise RuntimeError("Reader did not confirm storage contracts")
    result.update(
        context=args.context,
        namespace=NAMESPACE,
        image=args.image,
        storage_class=pvc["spec"].get("storageClassName"),
        access_modes=pvc["spec"]["accessModes"],
        pv_uid=pv["metadata"]["uid"],
        csi_driver=pv["spec"].get("csi", {}).get("driver"),
        writer_pod_uid=writer["metadata"]["uid"],
        recovery_pod_uid=recovery["metadata"]["uid"],
        reader_pod_uid=reader["metadata"]["uid"],
        writer_node=writer["spec"]["nodeName"],
        reader_node=reader["spec"]["nodeName"],
        cross_node_qualified=writer["spec"]["nodeName"] != reader["spec"]["nodeName"],
    )
    record(directory / "result.json", json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    lock = commands.add_parser("lock-image")
    lock.add_argument("--local-tag", required=True)
    lock.add_argument("--output", type=Path, default=ROOT / "var/k8s-storage-image.txt")
    execute = commands.add_parser("run")
    execute.add_argument("--image", required=True)
    execute.add_argument("--context", default="minikube")
    execute.add_argument("--storage-class")
    execute.add_argument(
        "--access-mode",
        choices=("ReadWriteOnce", "ReadWriteMany", "ReadWriteOncePod"),
        default="ReadWriteOnce",
    )
    execute.add_argument("--reader-node")
    execute.add_argument("--output", type=Path, default=ROOT / "var/k8s-storage-contracts")
    args = parser.parse_args()
    if args.operation == "lock-image":
        lock_image(args.local_tag, args.output)
    else:
        run(args)


if __name__ == "__main__":
    main()
