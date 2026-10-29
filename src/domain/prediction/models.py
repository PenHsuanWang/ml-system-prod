from dataclasses import dataclass


@dataclass(frozen=True)
class Prediction:
    class_id: int
    label: str
    score: float
    score_kind: str = "uncalibrated_softmax"


@dataclass(frozen=True)
class ServingDeployment:
    tenant_id: str
    endpoint_id: str
    deployment_id: str
    generation: int
    registered_model: str
    model_version: str


@dataclass(frozen=True)
class PredictionResponse:
    predictions: tuple[Prediction, ...]
    provenance: ServingDeployment
    request_id: str
