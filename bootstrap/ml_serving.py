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
from src.api.ebm_schemas import EBMPredictionRequest
from src.application.prediction.service import PredictionService, EBMPredictionService
from src.domain.ml import SchemaMismatch
from src.domain.prediction.models import ServingDeployment
from src.infrastructure.ml_identity import MongoActorResolver
from src.infrastructure.serving.bounded_prediction import BoundedPredictionExecutor
from src.infrastructure.serving.materializer import BundleMaterializer, verify_bundle
from src.infrastructure.serving.pytorch_runtime import PyTorchRuntime
from src.plugins.models.mlp import build_mlp
from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn
from src.infrastructure.serving.ebm_runtime import EBMRuntime
from src.infrastructure.serving.bounded_ebm_prediction import BoundedEBMPredictionExecutor


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
        manifest = verify_bundle(directory, spec["manifest_digest"])
        is_ebm = manifest.get("artifact", {}).get("profile") == "ebm-neural-bundle-v1"
        if runtime_factory:
            runtime = runtime_factory(directory, spec["manifest_digest"])
        elif is_ebm:
            runtime = EBMRuntime(directory, spec["manifest_digest"], build_ebm_cnn,
                expected_code_digest=settings.code_revision, max_batch=16)
        else:
            runtime = PyTorchRuntime(directory, spec["manifest_digest"], build_mlp,
                                     expected_code_digest=settings.code_revision)
        timeout = float(os.environ.get("ML_PREDICTION_TIMEOUT_SECONDS", "5"))
        executor = (BoundedEBMPredictionExecutor(runtime, latent_dim=runtime.mapping["latent_dim"],
                                                capacity=2, timeout=timeout) if is_ebm else
                    BoundedPredictionExecutor(runtime, capacity=2, timeout=timeout))
        deployment = ServingDeployment(spec["tenant_id"], spec["endpoint_id"], spec["deployment_id"],
                                       spec["generation"], spec["registered_name"], spec["model_version"])
        service = EBMPredictionService(executor, deployment) if is_ebm else PredictionService(executor, deployment)
        state.update(spec=spec, prediction=service, input_schema_version=(
            "ebm-spectrogram-input-v1" if is_ebm else "1"))
        app.state.ml_identity = provider
        try:
            yield
        finally:
            executor.close()
            if identity is None:
                provider.close()

    app = FastAPI(title="Fixed Model Serving", lifespan=lifespan)
    install_errors(app, maximum_body_bytes=8 * 1024 * 1024)
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
    def predict(body: PredictionRequest | EBMPredictionRequest,
                actor: ActorDependency, request: Request) -> object:
        if body.schema_version != state["input_schema_version"]:
            raise SchemaMismatch("Request does not match the deployed model input contract")
        values = (body.to_domain() if isinstance(body, EBMPredictionRequest) else
                  tuple((item.features.sensor_a, item.features.sensor_b) for item in body.instances))
        with operation("ml.prediction", attributes={"ml.deployment_id": state["spec"]["deployment_id"],
                                                    "ml.generation": state["spec"]["generation"]}):
            result = asdict(state["prediction"].predict(actor, body.endpoint_id, values))
        result["request_id"] = request.state.request_id
        result["provenance"].pop("tenant_id")
        result["provenance"]["input_schema_version"] = state["input_schema_version"]
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
