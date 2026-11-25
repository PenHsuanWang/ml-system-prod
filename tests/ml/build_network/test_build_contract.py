"""Portable build contracts, with no Docker daemon or corporate files required."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def instructions(name):
    source = (ROOT / name).read_text().replace("\\\n", " ")
    return re.findall(r"(?m)^RUN (.*)$", source)


@pytest.mark.parametrize("name", ["Dockerfile.ml", "Dockerfile.mlflow", "Dockerfile.minio"])
def test_all_network_steps_receive_optional_verified_trust(name):
    commands = instructions(name)
    network = [
        c
        for c in commands
        if any(x in c for x in ("pip install", "apt-get ", "apk add", "go install"))
    ]
    assert network
    for command in network:
        assert "--mount=type=secret,id=corporate_ca,required=false" in command
        assert "--mount=type=bind,from=build-trust" in command
        for tool in (
            "pip install",
            "apt-get update",
            "apt-get install",
            "apk add",
            "go install",
            "go list",
        ):
            for match in re.finditer(re.escape(tool), command):
                assert command[: match.start()].endswith("/tmp/build-trust ")
    source = (ROOT / name).read_text()
    for forbidden in (
        "Verify-Peer=false",
        "Verify-Host=false",
        "--insecure",
        "--trusted-host",
        "GIT_SSL_NO_VERIFY",
        "GOINSECURE",
        "GOSUMDB=off",
        "COPY corporate",
        "ARG HTTP_PROXY",
        "ENV HTTP_PROXY",
    ):
        assert forbidden not in source
    assert "COPY --from=build-trust" not in source
    assert "ENV SSL_CERT_FILE" not in source


def test_minio_sources_licenses_and_labels_preserved():
    source = (ROOT / "Dockerfile.minio").read_text()
    assert "MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a" in source
    assert "MC_COMMIT=7394ce0dd2a80935aded936b09fa12cbb3cb8096" in source
    for identity in ("minio", "mc"):
        assert f'org.opencontainers.image.source="https://github.com/minio/{identity}"' in source
        assert f"COPY --from=build /licenses/{identity} /licenses" in source
    assert "AS server" in source and "AS client" in source


def test_ignored_private_inputs_are_also_excluded_from_context(tmp_path):
    assert "/.local/corporate-network/" in (ROOT / ".dockerignore").read_text().splitlines()
    assert "/.local/corporate-network/" in (ROOT / ".gitignore").read_text().splitlines()
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            ".local/corporate-network/ca.pem",
            ".local/corporate-network/proxy.env",
            ".local/corporate-network/private.key",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and len(result.stdout.splitlines()) == 3
    # Existing certificate fixtures outside that directory must remain available.
    assert not any(
        line in {"*.pem", "*.crt", "*.key"}
        for line in (ROOT / ".dockerignore").read_text().splitlines()
    )


def test_runtime_target_inventory_unchanged():
    stages = re.findall(r"(?mi)^FROM .+ AS ([\w-]+)$", (ROOT / "Dockerfile.ml").read_text())
    assert set(stages) == {
        "build-trust",
        "api",
        "ingestion",
        "ingestion-test",
        "training",
        "acceptance",
        "ebm",
        "production",
        "k8s-acceptance",
        "serving",
    }
