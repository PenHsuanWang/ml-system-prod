"""Materialize and verify the developer guide in fresh, isolated project copies.

The reviewed guide has 22 Python blocks with explicit file bindings below.
An added/reordered block fails rather than silently validating different code.
No teaching capability is registered in the real project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    0: ("src/domain/items/models.py", "class Item:"),
    1: ("src/domain/items/errors.py", "class ItemNotFound"),
    2: ("src/domain/items/policy.py", "def normalize_name"),
    3: ("src/domain/items/ports.py", "class ItemRepository"),
    4: ("src/application/items/service.py", "class ItemService"),
    5: ("src/infrastructure/persistence/items.py", "class MongoItemRepository"),
    6: ("src/plugins/heuristics/items_scoring.py", "class ItemScoringKernel"),
    7: ("src/api/items/schemas.py", "class CreateItemRequest"),
    8: ("src/api/items/dependencies.py", "ItemServiceDependency"),
    9: ("src/api/items/errors.py", "def item_http_error"),
    10: ("src/api/items/routers.py", "router = APIRouter"),
    11: ("src/config/items.py", "class ItemsSettings"),
    12: ("bootstrap/items.py", "def build_items_module"),
    14: ("src/application/items/tasks.py", "class ItemReadHandler"),
    15: ("src/application/items/jobs.py", "class ItemReadJobService"),
    16: ("src/application/items/facade.py", "class ItemServices"),
    17: ("bootstrap/items.py", "def build_items_module"),
    18: ("src/api/items/dependencies.py", "ItemReadJobServiceDependency"),
    20: ("src/api/items/routers.py", "router = APIRouter"),
    21: ("tests/test_items_documented.py", "import pytest"),
}


def write_module(project: Path, relative: str, body: str) -> None:
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    parent = target.parent
    while parent != project and parent.name != "tests":
        (parent / "__init__.py").touch(exist_ok=True)
        parent = parent.parent


def materialize(project: Path, blocks: list[str], mode: str) -> None:
    # Explicit allowlist excludes .env, local accounts, Git, caches and artifacts.
    for directory in ("src", "bootstrap", "tests", "scripts"):
        shutil.copytree(
            ROOT / directory,
            project / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for pattern in ("*.lock", "requirements*.txt", "pyproject.toml", "README.md", "*.py"):
        for source in ROOT.glob(pattern):
            if source.is_file():
                shutil.copy2(source, project / source.name)
    for index, body in enumerate(blocks):
        if mode == "sync" and 14 <= index <= 20:
            continue
        if index == 13:
            if "items_settings = ItemsSettings()" not in body:
                raise ValueError("Guide wiring block changed; review the file mapping")
            path = project / "bootstrap/wiring.py"
            original = path.read_text(encoding="utf-8")
            old_import = "from src.config.settings import Settings"
            marker = "    # Add approved cross-layer application initializers here.\n"
            if original.count(old_import) != 1 or original.count(marker) != 1:
                raise ValueError("Composition root changed; review the guide wiring instruction")
            original = original.replace(
                old_import, "from src.config.items import ItemsSettings\n" + old_import
            ).replace(marker, marker + body)
            path.write_text(original, encoding="utf-8")
        elif index == 19:
            if not body.startswith("class ItemReadRunRequest"):
                raise ValueError("Guide schema append block changed")
            path = project / "src/api/items/schemas.py"
            path.write_text(path.read_text(encoding="utf-8") + "\n\n" + body, encoding="utf-8")
        else:
            relative, signature = FILES[index]
            if signature not in body:
                raise ValueError(f"Guide block {index} no longer matches {relative}")
            write_module(project, relative, body)
    evidence = ROOT / "docs/evidence/developer-guide-2026-10-24"
    for filename in (
        "test_items_contract.py",
        *(("test_items_background_contract.py",) if mode == "background" else ()),
    ):
        shutil.copy2(evidence / (filename + ".txt"), project / "tests" / filename)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "work/guide-validation")
    parser.add_argument("--materialize-only", action="store_true")
    args = parser.parse_args()
    guide = ROOT / "docs/developer-guide.md"
    document = guide.read_text(encoding="utf-8")
    blocks = [body + "\n" for body in re.findall(r"```python\n(.*?)\n```", document, re.S)]
    if len(blocks) != 22:
        raise ValueError(f"Expected 22 reviewed Python blocks; found {len(blocks)}")
    output = args.output_dir.resolve() / uuid4().hex[:12]
    output.mkdir(parents=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("APP_")}
    environment.pop("RUN_SYSTEM_INTEGRATION", None)
    summary: dict[str, object] = {
        "guide_sha256": hashlib.sha256(guide.read_bytes()).hexdigest(),
        "blocks": len(blocks),
        "materialize_only": args.materialize_only,
        "checks": [],
    }
    records: list[dict[str, object]] = []
    for mode in ("sync", "background"):
        project = output / mode
        materialize(project, blocks, mode)
        if args.materialize_only:
            print(project, flush=True)
            continue
        tests = ["tests/test_items_documented.py", "tests/test_items_contract.py"]
        if mode == "background":
            tests.append("tests/test_items_background_contract.py")
        checks = [
            ("boundaries", [sys.executable, "scripts/check_boundaries.py"]),
            ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
            ("format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
            (
                "mypy",
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "src",
                    "bootstrap",
                    "scripts",
                    "server.py",
                    "worker.py",
                    "manage.py",
                ],
            ),
            ("tests", [sys.executable, "-m", "pytest", "-q", *tests]),
        ]
        for name, command in checks:
            completed = subprocess.run(
                command,
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            log = f"{mode}-{name}.log"
            (output / log).write_text(completed.stdout + completed.stderr, encoding="utf-8")
            records.append(
                {"mode": mode, "name": name, "exit_code": completed.returncode, "log": log}
            )
            print(f"{mode} {name}: {'PASS' if completed.returncode == 0 else 'FAIL'}", flush=True)
    summary["checks"] = records
    summary["passed"] = not args.materialize_only and all(
        item["exit_code"] == 0 for item in records
    )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence: {output}", flush=True)
    return 0 if args.materialize_only or summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
