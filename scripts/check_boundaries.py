"""Check static imports against the architecture's physical package boundaries.

Run ``python scripts/check_boundaries.py [project-root]``. Production sources are
``src/``, ``bootstrap/``, and the ``server.py``, ``worker.py``, and ``manage.py``
entrypoints. Absolute and relative imports in all scopes, including TYPE_CHECKING
blocks, are inspected without executing application code. Tests are not scanned.

The layer matrix permits arbitrary capability packages within each layer. Only
bootstrap and entrypoints may assemble concrete implementations. The API's
``src.api.dependencies`` provider may use ``bootstrap.container``; that module
must expose dependency access rather than application workflows. This static
check cannot inspect re-exported object behavior, dynamic imports, plugin I/O,
or third-party transitive imports. Explicit registration and dependency review
remain necessary.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

LAYERS = frozenset(
    {
        "api",
        "services",
        "application",
        "domain",
        "plugins",
        "infrastructure",
        "config",
        "utils",
        "shared_models",
    }
)
STANDARD_LIBRARY = sys.stdlib_module_names | {"__future__"}
ENTRYPOINTS = frozenset({"server", "worker", "manage"})

# These modules expose framework/transport/storage APIs, not scientific strategy
# contracts. This is deliberately a denylist rather than a plugin sandbox.
PLUGIN_ADAPTER_LIBRARIES = frozenset(
    {
        "aio_pika",
        "aiohttp",
        "boto3",
        "botocore",
        "celery",
        "fastapi",
        "grpc",
        "httpx",
        "kafka",
        "motor",
        "mlflow",
        "opentelemetry",
        "pika",
        "pydantic",
        "pydantic_settings",
        "pymongo",
        "redis",
        "requests",
        "sqlalchemy",
        "sqlmodel",
        "starlette",
        "taskiq",
        "taskiq_aio_pika",
        "uvicorn",
    }
)

LAYER_DEPENDENCIES: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain", "shared_models"}),
    "application": frozenset({"application", "domain", "shared_models", "utils"}),
    "plugins": frozenset({"plugins", "domain", "shared_models", "utils"}),
    "infrastructure": frozenset({"infrastructure", "domain", "shared_models", "utils", "config"}),
    "services": frozenset(
        {"services", "config", "domain", "shared_models", "utils", "infrastructure"}
    ),
    "api": frozenset({"api", "application", "domain", "shared_models", "utils", "services"}),
    "config": frozenset({"config", "shared_models"}),
    "utils": frozenset({"utils", "shared_models"}),
    "shared_models": frozenset({"shared_models", "utils"}),
}
PURE_LAYERS = frozenset({"domain", "application", "utils", "shared_models"})


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str
    target: str = ""

    def __str__(self) -> str:
        detail = f" ({self.target})" if self.target else ""
        return f"{self.path}:{self.line}: {self.message}{detail}"


@dataclass(frozen=True)
class Source:
    path: Path
    module: str
    package: str
    layer: str | None
    composition: bool


def _under(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def _source(path: Path, root: Path) -> Source:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    module = ".".join(parts)
    package = module if is_package else ".".join(parts[:-1])
    layer = parts[1] if len(parts) >= 2 and parts[0] == "src" else None
    composition = parts[0] == "bootstrap" or (len(parts) == 1 and parts[0] in ENTRYPOINTS)
    return Source(path, module, package, layer, composition)


def _imports(tree: ast.AST, package: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = package.split(".") if package else []
                if node.level > len(package_parts):
                    yield node.lineno, ""
                    continue
                base_parts = package_parts[: len(package_parts) - node.level + 1]
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            for alias in node.names:
                # Resolve the imported name to catch ``from src import infrastructure``
                # and aliases of a package as well as ordinary module imports.
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                yield node.lineno, target


def _reason(source: Source, target: str) -> str | None:
    if not target:
        return "relative import escapes its package"
    external_root = target.split(".", 1)[0]
    if external_root == "backend":
        return "legacy backend imports are forbidden; use the physical src/bootstrap packages"
    if source.composition:
        return None
    if external_root in ENTRYPOINTS or external_root in {"tests", "scripts"}:
        return "production layers must not depend on entrypoints or development tooling"
    if external_root == "bootstrap":
        if source.module == "src.api.dependencies" and _under(target, "bootstrap.container"):
            return None
        return "only src.api.dependencies may import bootstrap.container"
    if external_root == "src":
        parts = target.split(".")
        target_layer = parts[1] if len(parts) >= 2 else ""
        allowed = LAYER_DEPENDENCIES.get(source.layer or "", frozenset())
        if target_layer not in allowed:
            label = source.layer or "src package root"
            return f"{label} may not import the {target_layer or 'src root'} layer"
        return None
    if external_root in STANDARD_LIBRARY:
        return None
    if source.layer in PURE_LAYERS or source.module == "src":
        return (
            f"{source.layer or 'src package root'} may import only "
            "standard-library and allowed internal contracts"
        )
    if source.layer == "plugins" and external_root in PLUGIN_ADAPTER_LIBRARIES:
        return "plugins must not import known transport/storage/framework adapters"
    return None


def _production_sources(root: Path) -> Iterator[Path]:
    for directory in ("src", "bootstrap"):
        base = root / directory
        if base.is_dir():
            yield from base.rglob("*.py")
    for entrypoint in sorted(ENTRYPOINTS):
        path = root / f"{entrypoint}.py"
        if path.is_file():
            yield path


def check_boundaries(root: Path) -> list[Violation]:
    """Return violations without importing or executing production source code."""
    root = root.resolve()
    if not (root / "src").is_dir():
        return [Violation(root / "src", 1, "src source directory does not exist")]

    violations: list[Violation] = []
    legacy = root / "backend"
    if legacy.is_dir() and any(legacy.rglob("*.py")):
        violations.append(Violation(legacy, 1, "legacy backend production package must be removed"))
    for path in sorted(_production_sources(root)):
        source = _source(path, root)
        if source.module != "src" and not source.composition and source.layer not in LAYERS:
            violations.append(
                Violation(path, 1, "src source must live in a declared architecture layer")
            )
        try:
            with tokenize.open(path) as stream:
                tree = ast.parse(stream.read(), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            line = exc.lineno or 1 if isinstance(exc, SyntaxError) else 1
            violations.append(Violation(path, line, f"cannot parse Python source: {exc}"))
            continue
        for line, target in _imports(tree, source.package):
            reason = _reason(source, target)
            if reason:
                violations.append(Violation(path, line, reason, target))
    return sorted(violations, key=lambda item: (str(item.path), item.line, item.target))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args(argv)
    violations = check_boundaries(arguments.root)
    for violation in violations:
        print(violation)
    if violations:
        print(f"Dependency boundary check failed: {len(violations)} violation(s).")
        return 1
    print("Dependency boundaries passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
