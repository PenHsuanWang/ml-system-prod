from uuid import uuid4

from src.application.prediction.ports import PredictionExecutor
from src.domain.ml import Actor, NotFound
from src.domain.prediction.models import PredictionResponse, ServingDeployment


class PredictionService:
    def __init__(self, executor: PredictionExecutor, deployment: ServingDeployment) -> None:
        self.executor = executor
        self.deployment = deployment

    def predict(self, actor: Actor, endpoint: str,
                values: tuple[tuple[float, float], ...]) -> PredictionResponse:
        actor.require("ml:predict")
        if actor.tenant_id != self.deployment.tenant_id or endpoint != self.deployment.endpoint_id:
            raise NotFound("Endpoint not found")
        return PredictionResponse(self.executor.predict(values), self.deployment, str(uuid4()))
