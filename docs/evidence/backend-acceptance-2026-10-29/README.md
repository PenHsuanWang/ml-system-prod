# Backend acceptance evidence

See the [acceptance report](../../backend-acceptance-2026-10-29.md). Result files describe executed local evidence, not real-service qualification.

- `result.json`, `junit.xml` and individual check logs: final executable checks.
- `regressions-before.xml`: five reproduced failures before their fixes.
- `regressions-after.xml`: the original ten fault/guard cases after fixes.
- `required-stack-rejection.json`: fail-closed preflight with missing isolated endpoints (exit 2).
- `environment.json`: repeated environment probes and the full-template collection limitation.

Executed local command:

```sh
PYTHONPATH=/tmp/ml-ebm-deps:. python scripts/verify_ml_acceptance.py \
  --host-selector-workaround --output-dir /tmp/ml-backend-acceptance-final
```

The selector workaround is documented in [earlier CPU evidence](../ml-cpu-2026-10-29/README.md). It does not change the application. Three real integration cases were skipped; the real stack runner requires them and rejects skipped cases. No Docker image or remote CI result is claimed.
