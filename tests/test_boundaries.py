from pathlib import Path

import pytest

from scripts.check_boundaries import LAYERS, check_boundaries, main


def write_source(root: Path, relative: str, content: str = "") -> Path:
    (root / "src").mkdir(exist_ok=True)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("src/domain/models/identifier.py", "from dataclasses import dataclass\n"),
        ("src/domain/strategy/kernel.py", "from ..models.identifier import Identifier\n"),
        ("src/domain/ports/storage.py", "from src.shared_models import Payload\n"),
        ("src/application/use_cases/evaluate.py", "from src.domain.strategy import Kernel\n"),
        ("src/application/use_cases/evaluate.py", "from ..registry import Registry\n"),
        ("src/application/orchestration/run.py", "from src.utils.json import validate\n"),
        ("src/plugins/inference/evaluate.py", "import numpy as np\n"),
        ("src/plugins/heuristics/evaluate.py", "from scipy.signal import find_peaks\n"),
        ("src/plugins/interceptors/normalize.py", "from src.domain.strategy import Interceptor\n"),
        ("src/plugins/inference/evaluate.py", "from ..interceptors.normalize import normalize\n"),
        ("src/infrastructure/persistence/mongo.py", "from src.domain.ports import Repository\n"),
        ("src/infrastructure/messaging/taskiq.py", "import taskiq\n"),
        ("src/infrastructure/locking/redis.py", "from src.config.settings import Settings\n"),
        (
            "src/services/lifecycle/resources.py",
            "from src.infrastructure.persistence import Mongo\n",
        ),
        ("src/services/concurrency/threads.py", "from src.domain.ports import ThreadRunner\n"),
        ("src/api/routers/identity.py", "from src.application.use_cases import IdentityService\n"),
        ("src/api/schemas/identity.py", "from pydantic import BaseModel\n"),
        ("src/api/dependencies.py", "from bootstrap.container import get_runtime\n"),
        ("src/api/dependencies.py", "from bootstrap import container\n"),
        ("src/api/dependencies.py", "import bootstrap.container as dependencies\n"),
        ("src/api/exceptions.py", "from src.services.observability import request_id\n"),
        ("src/config/settings.py", "from pydantic_settings import BaseSettings\n"),
        ("src/shared_models/payload.py", "from typing import TypeAlias\n"),
        ("src/shared_models/payload.py", "from src.utils.json import JsonValue\n"),
        ("src/utils/json.py", "from src.shared_models.payload import Payload\n"),
        ("bootstrap/plugin_loader.py", "from src.plugins.inference import Kernel\n"),
        ("bootstrap/wiring.py", "from src.application.use_cases import Service\n"),
        ("bootstrap/container.py", "from src.services.lifecycle import Runtime\n"),
        ("server.py", "from bootstrap.wiring import build_application\n"),
        ("worker.py", "from src.infrastructure.messaging.taskiq import build_broker\n"),
        ("manage.py", "from src.application.use_cases.identity import IdentityService\n"),
    ],
)
def test_allowed_imports(tmp_path: Path, relative: str, content: str) -> None:
    write_source(tmp_path, relative, content)
    assert check_boundaries(tmp_path) == []


@pytest.mark.parametrize(
    ("source_layer", "target_layer"),
    [
        ("domain", "application"),
        ("domain", "config"),
        ("domain", "utils"),
        ("domain", "plugins"),
        ("application", "infrastructure"),
        ("application", "services"),
        ("application", "plugins"),
        ("application", "config"),
        ("plugins", "application"),
        ("plugins", "infrastructure"),
        ("plugins", "api"),
        ("plugins", "services"),
        ("plugins", "config"),
        ("infrastructure", "application"),
        ("infrastructure", "plugins"),
        ("infrastructure", "services"),
        ("infrastructure", "api"),
        ("services", "application"),
        ("services", "plugins"),
        ("services", "api"),
        ("api", "infrastructure"),
        ("api", "plugins"),
        ("api", "config"),
        ("config", "domain"),
        ("config", "application"),
        ("config", "utils"),
        ("utils", "domain"),
        ("utils", "config"),
        ("shared_models", "domain"),
        ("shared_models", "config"),
    ],
)
def test_layer_matrix_rejects_outward_imports(
    tmp_path: Path, source_layer: str, target_layer: str
) -> None:
    path = write_source(
        tmp_path,
        f"src/{source_layer}/arbitrary_capability/nested/component.py",
        f"from src.{target_layer} import implementation\n",
    )
    violations = check_boundaries(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == path
    assert violations[0].line == 1
    assert f"{source_layer} may not import the {target_layer} layer" in violations[0].message


@pytest.mark.parametrize("layer", ["domain", "application", "shared_models", "utils"])
@pytest.mark.parametrize("dependency", ["fastapi", "pydantic", "httpx", "numpy", "unknown_sdk"])
def test_pure_layers_reject_external_packages(tmp_path: Path, layer: str, dependency: str) -> None:
    write_source(tmp_path, f"src/{layer}/component.py", f"import {dependency}\n")
    violations = check_boundaries(tmp_path)
    assert len(violations) == 1
    assert "only standard-library" in violations[0].message


@pytest.mark.parametrize(
    "dependency",
    ["fastapi", "pydantic", "redis", "taskiq", "httpx", "pymongo", "sqlalchemy", "boto3"],
)
def test_plugins_reject_known_frameworks_and_clients(tmp_path: Path, dependency: str) -> None:
    write_source(tmp_path, "src/plugins/inference/strategy.py", f"import {dependency}\n")
    violations = check_boundaries(tmp_path)
    assert len(violations) == 1
    assert "known transport/storage/framework" in violations[0].message


@pytest.mark.parametrize("layer", sorted(LAYERS))
@pytest.mark.parametrize("target", ["bootstrap.wiring", "bootstrap.plugin_loader", "bootstrap"])
def test_layers_cannot_import_composition_root(tmp_path: Path, layer: str, target: str) -> None:
    write_source(tmp_path, f"src/{layer}/component.py", f"import {target}\n")
    violations = check_boundaries(tmp_path)
    assert len(violations) == 1
    assert "only src.api.dependencies" in violations[0].message


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_container_exception_is_limited_to_api_dependencies(tmp_path: Path, layer: str) -> None:
    write_source(tmp_path, f"src/{layer}/component.py", "from bootstrap.container import Runtime\n")
    assert len(check_boundaries(tmp_path)) == 1


@pytest.mark.parametrize(
    "relative",
    [
        "src/domain/models.py",
        "src/application/use_cases.py",
        "src/plugins/inference.py",
        "src/infrastructure/persistence.py",
        "src/services/lifecycle.py",
        "src/api/dependencies.py",
        "bootstrap/container.py",
        "server.py",
        "worker.py",
        "manage.py",
    ],
)
@pytest.mark.parametrize("statement", ["import backend", "from backend.core import config"])
def test_legacy_backend_imports_are_forbidden_everywhere(
    tmp_path: Path, relative: str, statement: str
) -> None:
    write_source(tmp_path, relative, statement + "\n")
    violations = check_boundaries(tmp_path)
    assert len(violations) == 1
    assert "legacy backend imports are forbidden" in violations[0].message


@pytest.mark.parametrize("target", ["server", "worker", "manage", "tests.helpers", "scripts.tool"])
def test_layers_cannot_import_entrypoints_or_tooling(tmp_path: Path, target: str) -> None:
    write_source(tmp_path, "src/infrastructure/adapter.py", f"import {target}\n")
    assert "entrypoints or development tooling" in check_boundaries(tmp_path)[0].message


def test_relative_and_package_alias_imports_cannot_bypass_matrix(tmp_path: Path) -> None:
    write_source(
        tmp_path,
        "src/application/use_cases/nested/evaluate.py",
        "from ....infrastructure import adapter\n"
        "from src import plugins as implementations\n"
        "from .... import services\n",
    )
    violations = check_boundaries(tmp_path)
    assert [item.target for item in violations] == [
        "src.infrastructure.adapter",
        "src.plugins",
        "src.services",
    ]


def test_checks_nested_and_type_checking_imports(tmp_path: Path) -> None:
    write_source(
        tmp_path,
        "src/application/use_cases/service.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from src.infrastructure import adapter\n"
        "def factory():\n"
        "    import pydantic\n",
    )
    assert [violation.line for violation in check_boundaries(tmp_path)] == [3, 5]


def test_src_root_cannot_reexport_outer_layers(tmp_path: Path) -> None:
    write_source(tmp_path, "src/__init__.py", "from .api import create_router\n")
    assert "src package root may not import" in check_boundaries(tmp_path)[0].message


def test_undeclared_layer_and_syntax_error_are_reported(tmp_path: Path) -> None:
    write_source(tmp_path, "src/helpers.py", "def invalid(:\n")
    violations = check_boundaries(tmp_path)
    assert len(violations) == 2
    assert any("cannot parse" in violation.message for violation in violations)
    assert any("declared architecture layer" in violation.message for violation in violations)


def test_relative_import_cannot_escape_package(tmp_path: Path) -> None:
    write_source(tmp_path, "src/domain/__init__.py", "from .... import invalid\n")
    assert "escapes its package" in check_boundaries(tmp_path)[0].message


def test_rejects_leftover_backend_production_tree(tmp_path: Path) -> None:
    write_source(tmp_path, "src/__init__.py")
    write_source(tmp_path, "backend/__init__.py")
    violations = check_boundaries(tmp_path)
    assert len(violations) == 1
    assert "legacy backend production package must be removed" in violations[0].message


def test_test_fixtures_and_development_tools_are_not_production(tmp_path: Path) -> None:
    write_source(tmp_path, "src/__init__.py")
    write_source(tmp_path, "tests/fixtures/src/domain/model.py", "import fastapi\n")
    write_source(tmp_path, "scripts/example.py", "import backend\n")
    assert check_boundaries(tmp_path) == []


def test_production_source_respects_boundaries() -> None:
    assert check_boundaries(Path(__file__).resolve().parents[1]) == []


def test_cli_reports_failure_with_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_source(tmp_path, "src/domain/models.py", "import pydantic\n")
    assert main([str(tmp_path)]) == 1
    assert f"{path}:1:" in capsys.readouterr().out


def test_cli_reports_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_source(tmp_path, "src/__init__.py")
    assert main([str(tmp_path)]) == 0
    assert "Dependency boundaries passed." in capsys.readouterr().out


def test_missing_production_tree_is_a_failure(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 1
