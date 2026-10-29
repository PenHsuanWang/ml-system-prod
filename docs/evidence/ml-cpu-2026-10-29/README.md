# Local CPU smoke evidence

These results validate the local CPU implementation, not the target Python 3.12/Torch 2.6 image or production. See [implementation status](../../implementation-status.md) for unexecuted qualification.

- [Machine-readable result](result.json) and [JUnit report](junit.xml).
- Real child-process training, held-out exported-bundle evaluation, prediction, checkpoint/resume and physical cancellation were exercised.
- Custom data with inverted labels trained a different predictor; lineage binds its complete snapshot and training-spec digests.
- Race checks cover idempotency, lease/owner fencing, terminal-state guards, cancellation and delayed activation; controller tests cover interrupted phases and retirement capacity.
- The two real Receiver cases require RabbitMQ and Redis and were skipped locally. CI supplies those services and a PostgreSQL profile; no remote execution is claimed.

The host has Python 3.10.12 and only cached Torch 2.0.1+cu117 available. Dependencies were extracted into `/tmp/ml-ebm-deps`; computation used CPU. ASGI tests require a host workaround because asyncio self-pipe writes do not promptly wake this host's selector. [host_selector_plugin.py](host_selector_plugin.py) is the exact test-only workaround; application event loops are unchanged.

The executed command used the identical plugin at `/tmp/ml_ebm_test_loop.py`:

```sh
PYTHONPATH=/tmp:/tmp/ml-ebm-deps:. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest tests/ml tests/test_boundaries.py -q \
  -p pytest_asyncio.plugin -p ml_ebm_test_loop \
  -o faulthandler_timeout=30 --junitxml=/tmp/ml-ebm-tests.xml
PYTHONPATH=/tmp/ml-ebm-deps:. python -m bootstrap.ml_cli demo
```

The target CI uses the ordinary event loop and supported dependencies. Other checks: source compilation, no Torch import during management module imports, 30 core source files under strict mypy, unused-import checks for new/changed source, and [Compose schema](https://github.com/compose-spec/compose-spec/blob/main/schema/compose-spec.json) validation. These checks do not replace container/service integration, dependency locks, security review, restore exercises or measured SLO acceptance.
