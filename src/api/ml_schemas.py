"""Public intent is distinct from the fully resolved immutable execution spec."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              protected_namespaces=())


class VersionRef(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)


class ModelConfig(StrictModel):
    hidden_units: list[Annotated[int, Field(ge=1, le=128)]] = Field(
        default_factory=lambda: [16, 8], min_length=1, max_length=3)


class ModelIntent(StrictModel):
    architecture: Literal["mlp_v1"] = "mlp_v1"
    config: ModelConfig = Field(default_factory=ModelConfig)


class Hyperparameters(StrictModel):
    epochs: int = Field(default=30, ge=1, le=100)
    batch_size: int = Field(default=64, ge=1, le=128)
    learning_rate: float = Field(default=0.01, gt=0, le=0.1)
    seed: int = Field(default=42, ge=0, le=2147483647)
    optimizer: Literal["adam"] = "adam"
    loss: Literal["cross_entropy"] = "cross_entropy"
    precision: Literal["fp32"] = "fp32"


class TrainingIntent(StrictModel):
    schema_version: Literal["1"] = "1"
    recipe: VersionRef
    dataset: VersionRef
    model: ModelIntent = Field(default_factory=ModelIntent)
    training: Hyperparameters = Field(default_factory=Hyperparameters)
    resource_profile_id: Literal["cpu-small-v1"] = "cpu-small-v1"


class SubmitRequest(StrictModel):
    spec: TrainingIntent
    retry_of_job_id: str | None = Field(default=None, min_length=1, max_length=64)


class ApprovalRequest(StrictModel):
    evaluation_id: str = Field(min_length=1, max_length=64)
    environment: Literal["dev", "staging", "prod"]


class DeploymentRequest(StrictModel):
    endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    model_binding_id: str = Field(min_length=1, max_length=64)
    approval_id: str = Field(min_length=1, max_length=64)


class Features(StrictModel):
    sensor_a: float = Field(ge=-100, le=100)
    sensor_b: float = Field(ge=-100, le=100)


class Instance(StrictModel):
    features: Features


class PredictionRequest(StrictModel):
    schema_version: Literal["1"] = "1"
    endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    instances: list[Instance] = Field(min_length=1, max_length=128)
