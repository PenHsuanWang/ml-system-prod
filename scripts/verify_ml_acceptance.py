"""Run reproducible Backend checks; full acceptance fails closed on skipped integration.

Use --host-selector-workaround only on the documented restricted development host.
Run --require-stack inside an isolated stack with real-service test URLs configured.
"""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CORE = ["src/domain/ml.py", "src/domain/training", "src/domain/deployments",
        "src/domain/model_registry", "src/domain/prediction", "src/domain/datasets",
        "src/application/training", "src/application/deployments", "src/application/model_registry",
        "src/application/prediction", "src/application/datasets", "src/application/ml_services.py"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT/"var/acceptance/latest")
    parser.add_argument("--require-stack", action="store_true")
    parser.add_argument("--host-selector-workaround", action="store_true")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output/"junit.xml").unlink(missing_ok=True)
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["MYPY_CACHE_DIR"] = str(output/"mypy-cache")
    environment["ML_REQUIRE_STACK_ACCEPTANCE"] = "1" if args.require_stack else "0"
    required = ("ML_TEST_DATABASE_URL", "ML_TEST_AMQP_URL", "ML_TEST_REDIS_URL",
                "ML_TEST_MANAGEMENT_URL", "ML_TEST_PREDICTION_URL")
    missing = [name for name in required if not environment.get(name)]
    if args.require_stack and missing:
        report = {"status": "FULL_BACKEND_NOT_ACCEPTED", "local_checks_passed": None,
                  "functional_backend_accepted": False, "production_accepted": False,
                  "checks": [], "acceptance_blockers": ["Missing isolated stack endpoint: "+name for name in missing]}
        (output/"result.json").write_text(json.dumps(report, indent=2)+"\n")
        print("FULL_BACKEND_NOT_ACCEPTED: required isolated stack endpoints are missing")
        return 2
    if not args.require_stack:
        for key in ("ML_TEST_DATABASE_URL", "ML_TEST_AMQP_URL", "ML_TEST_REDIS_URL",
                    "ML_TEST_MANAGEMENT_URL", "ML_TEST_PREDICTION_URL"):
            environment.pop(key, None)
    pytest = [sys.executable, "-m", "pytest", "tests/ml", "tests/test_boundaries.py", "-q",
              "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "-o", "faulthandler_timeout=45",
              "--junitxml="+str(output/"junit.xml")]
    if args.host_selector_workaround:
        plugin = ROOT/"docs/evidence/ml-cpu-2026-10-29"
        environment["PYTHONPATH"] = os.pathsep.join(filter(None,
                                                           [str(plugin), str(ROOT), environment.get("PYTHONPATH")]))
        pytest.extend(["-p", "host_selector_plugin"])
    lint_paths = sorted({str(path.relative_to(ROOT)) for pattern in (
        "bootstrap/ml_*.py", "src/domain/ml.py", "src/domain/training/*.py", "src/domain/deployments/*.py",
        "src/domain/model_registry/*.py", "src/domain/prediction/*.py", "src/domain/datasets/*.py",
        "src/application/training/*.py", "src/application/deployments/*.py", "src/application/model_registry/*.py",
        "src/application/prediction/*.py", "src/application/datasets/*.py", "src/application/ml_services.py",
        "src/api/ml_*.py", "src/infrastructure/ml_*.py", "src/infrastructure/persistence/ml_*.py",
        "src/infrastructure/messaging/ml_*.py", "src/infrastructure/datasets/*.py", "src/infrastructure/deployment/*.py",
        "src/infrastructure/execution/*.py", "src/infrastructure/serving/*.py", "src/infrastructure/mlflow/*.py",
        "src/plugins/models/*.py", "src/plugins/training/*.py", "tests/ml/**/*.py", "migrations/**/*.py",
        "scripts/verify_ml_acceptance.py") for path in ROOT.glob(pattern)})
    commands = [("boundaries", [sys.executable, "scripts/check_boundaries.py"]),
                ("core-typing", [sys.executable, "-m", "mypy", "--follow-imports=skip", *CORE]),
                ("unused-imports", [sys.executable, "-m", "pyflakes", *lint_paths]),
                ("compile", [sys.executable, "-m", "compileall", "-q",
                             "bootstrap", "src", "migrations", "scripts", "tests/ml"]),
                ("pytest", pytest)]
    checks = []
    for name, command in commands:
        started = time.monotonic()
        with (output/(name+".log")).open("w") as stream:
            try:
                result = subprocess.run(command, cwd=ROOT, env=environment,
                                        stdout=stream, stderr=subprocess.STDOUT, timeout=900, check=False)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
        checks.append({"name": name, "exit_code": code, "passed": code == 0,
                       "elapsed_seconds": round(time.monotonic()-started, 3), "log": name+".log"})
        print(name+": "+("PASS" if code == 0 else "FAIL"), flush=True)
    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    if (output/"junit.xml").exists():
        root = ET.parse(output/"junit.xml").getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        counts = {name: sum(int(suite.get(name, "0")) for suite in suites) for name in counts}
    counts["passed"] = counts["tests"]-counts["failures"]-counts["errors"]-counts["skipped"]
    try:
        torch = importlib.metadata.version("torch")
    except importlib.metadata.PackageNotFoundError:
        torch = None
    blockers = []
    if sys.version_info[:2] != (3, 12):
        blockers.append("Target Python 3.12 was not used")
    if torch is None or torch.split("+")[0] != "2.6.0":
        blockers.append("Target Torch 2.6.0 was not used")
    if args.host_selector_workaround:
        blockers.append("Host-specific test event-loop workaround was used")
    if not args.require_stack or any(not environment.get(key) for key in required):
        blockers.append("Required isolated stack endpoints were not all enabled")
    if counts["skipped"]:
        blockers.append("Required suite contains skipped cases")
    checks_passed = all(check["passed"] for check in checks)
    if not checks_passed:
        blockers.append("One or more executable checks failed")
    accepted = checks_passed and counts["tests"] > 0 and not blockers
    report = {"status": "FUNCTIONAL_BACKEND_ACCEPTED" if accepted else "FULL_BACKEND_NOT_ACCEPTED",
              "local_checks_passed": checks_passed, "functional_backend_accepted": accepted,
              "production_accepted": False, "python": sys.version.split()[0], "torch": torch,
              "pytest": counts, "checks": checks, "acceptance_blockers": blockers,
              "production_gates": ["Qualified immutable images/dependency locks and security evidence",
                                   "Approved capacity/SLO/retention/restore objectives and executed qualification"]}
    (output/"result.json").write_text(json.dumps(report, indent=2)+"\n")
    print(report["status"]+"; report: "+str(output/"result.json"))
    return 0 if accepted or checks_passed and not args.require_stack else 1


if __name__ == "__main__":
    raise SystemExit(main())
