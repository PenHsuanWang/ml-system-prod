"""One immutable verified deployment per process; no App DB or registry lookup per prediction."""

import argparse
import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request

from bootstrap.ml_config import MLSettings
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import MLTraceMiddleware, operation
from src.api.ml_http import ActorDependency, install_errors
from src.api.ml_schemas import PredictionRequest
from src.application.prediction.service import PredictionService
from src.domain.prediction.models import ServingDeployment
from src.infrastructure.ml_identity import MongoActorResolver
from src.infrastructure.serving.bounded_prediction import BoundedPredictionExecutor
from src.infrastructure.serving.materializer import BundleMaterializer
from src.infrastructure.serving.pytorch_runtime import PyTorchRuntime
from src.plugins.models.mlp import build_mlp


def create_app(*, spec_path: Path | None = None, expected_digest: str | None = None,
               identity: object | None = None, runtime_factory: object | None = None) -> FastAPI:
    path = spec_path or Path(os.environ["ML_DEPLOYMENT_SPEC_PATH"])
    trusted_digest = expected_digest or os.environ["ML_DEPLOYMENT_SPEC_DIGEST"]
    settings = MLSettings.from_env()
    state = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_telemetry("serving")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != trusted_digest:
            raise ValueError("Deployment spec digest mismatch")
        spec = json.loads(data)
        if spec["runtime_image_digest"] != settings.image_digest:
            raise ValueError("Running image does not match deployment qualification")
        provider = identity or MongoActorResolver(os.environ["APP_MONGO_URI"],
                                                  os.environ.get(
                                                      "APP_MONGO_DATABASE", "backend"), os.environ["APP_JWT_SECRET"],
                                                  os.environ.get("APP_JWT_ISSUER", "backend"), os.environ.get("APP_JWT_AUDIENCE", "backend-api"))
        directory = BundleMaterializer(settings.artifact_root, settings.tracking_uri).materialize(
            spec["artifact_ref"], spec["manifest_digest"])
        runtime = runtime_factory(directory, spec["manifest_digest"]) if runtime_factory else (
            PyTorchRuntime(directory, spec["manifest_digest"], build_mlp,
                           expected_code_digest=settings.code_revision))
        executor = BoundedPredictionExecutor(runtime, capacity=2,
                                             timeout=float(os.environ.get("ML_PREDICTION_TIMEOUT_SECONDS", "5")))
        deployment = ServingDeployment(spec["tenant_id"], spec["endpoint_id"], spec["deployment_id"],
                                       spec["generation"], spec["registered_name"], spec["model_version"])
        state.update(spec=spec, prediction=PredictionService(executor, deployment))
        app.state.ml_identity = provider
        try:
            yield
        finally:
            executor.close()
            if identity is None:
                provider.close()

    app = FastAPI(title="Fixed Model Serving", lifespan=lifespan)
    install_errors(app)
    app.add_middleware(MLTraceMiddleware)

    @app.get("/health/live")
    def live() -> dict[str, bool]:
        return {"live": True}

    @app.get("/health/ready")
    def ready() -> dict[str, object]:
        return {"ready": bool(state), "deployment_id": state["spec"]["deployment_id"],
                "generation": state["spec"]["generation"],
                "manifest_digest": state["spec"]["manifest_digest"]}

    @app.post("/api/v1/prediction/predict")
    def predict(body: PredictionRequest, actor: ActorDependency, request: Request) -> object:
        values = tuple((item.features.sensor_a, item.features.sensor_b) for item in body.instances)
        with operation("ml.prediction", attributes={"ml.deployment_id": state["spec"]["deployment_id"],
                                                    "ml.generation": state["spec"]["generation"]}):
            result = asdict(state["prediction"].predict(actor, body.endpoint_id, values))
        result["request_id"] = request.state.request_id
        result["provenance"].pop("tenant_id")
        result["provenance"]["input_schema_version"] = "1"
        return result

    return app


def main() -> None:
    import uvicorn
    from src.infrastructure.execution.local_process import process_identity
    directory = os.environ.get("ML_SERVING_EXECUTION_DIRECTORY")
    if directory:
        (Path(directory)/"process.json").write_text(json.dumps(process_identity(os.getpid())))
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run("bootstrap.ml_serving:create_app", factory=True, host=args.host,
                port=args.port, workers=1, limit_concurrency=64, timeout_graceful_shutdown=30)


if __name__ == "__main__":
    main()
