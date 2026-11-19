"""Build/check the source manifest from Git-tracked files, excluding local data."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def manifest(root=ROOT):
    paths = (
        subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    entries = []
    for name in sorted(set(paths)):
        if not name or name == "FILE-MANIFEST.json":
            continue
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        if name.startswith(".env") and not name.endswith(".example"):
            raise ValueError("Private environment file must not be tracked")
        data = path.read_bytes()
        entries.append(
            {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return {
        "package": "ml-system-ebm Git-tracked source and documentation",
        "document_status": "Capabilities and qualification scope: docs/implementation-status.md",
        "files": entries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = manifest()
    path = ROOT / "FILE-MANIFEST.json"
    if args.check:
        if json.loads(path.read_text()) != expected:
            raise SystemExit("Source manifest is stale; run python scripts/update_manifest.py")
        print(f"Manifest valid: {len(expected['files'])} tracked files")
    else:
        path.write_text(json.dumps(expected, indent=2) + "\n")
        print(f"Manifest updated: {len(expected['files'])} tracked files")


if __name__ == "__main__":
    main()
