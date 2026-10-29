"""Regenerate the reviewable source manifest, excluding secrets and runtime data."""

import hashlib
import json
import os
from pathlib import Path

EXCLUDED_DIRECTORIES = frozenset({".git", ".agents", ".codex", ".aws", ".venv", "var",
                                  "work", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
                                  "build", "dist", "htmlcov"})


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    entries = []
    for directory, folders, names in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in EXCLUDED_DIRECTORIES
                            and not name.endswith(".egg-info") and not (Path(directory)/name).is_symlink())
        for name in sorted(names):
            path = Path(directory)/name
            relative = path.relative_to(root).as_posix()
            if (relative == "FILE-MANIFEST.json" or path.is_symlink() or
                name.endswith((".pyc", ".db", ".db-shm", ".db-wal", ":Zone.Identifier")) or
                name in {".coverage", ".DS_Store"} or
                    name.startswith(".env") and not name.endswith(".example")):
                continue
            data = path.read_bytes()
            entries.append({"path": relative, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
    value = {"package": "ML System EBM CPU MVP source and documentation",
             "document_status": "CPU MVP implemented; local smoke evidence only; target/production qualification pending",
             "files": sorted(entries, key=lambda entry: entry["path"])}
    (root/"FILE-MANIFEST.json").write_text(json.dumps(value, indent=2)+"\n", encoding="utf-8")
    print(f"Manifest updated: {len(entries)} files")


if __name__ == "__main__":
    main()
