"""Public intent is distinct from the fully resolved immutable execution spec."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.training.ebm import EBMTrainingConfig
from src.domain.ml import MLError


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


class EBMRecipeRef(StrictModel):
    id: Literal["ebm_poc_replay_v1"]
    version: Literal["1.0.0"]


class EBMCNNConfig(StrictModel):
    channels: list[Annotated[int, Field(ge=1, le=128)]] = Field(
        default_factory=lambda: [16, 32], min_length=1, max_length=4)
    kernel_sizes: list[list[Annotated[int, Field(ge=1, le=7)]]] = Field(
        default_factory=lambda: [[3, 3], [3, 3]], min_length=1, max_length=4)
    strides: list[list[Annotated[int, Field(ge=1, le=4)]]] = Field(
        default_factory=lambda: [[1, 1], [1, 1]], min_length=1, max_length=4)
    features_per_pad: int = Field(default=4, ge=1, le=64)
    dropout: float = Field(default=0.1, ge=0, lt=1)
    seed_size: list[Annotated[int, Field(ge=1, le=16)]] = Field(
        default_factory=lambda: [4, 4], min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_layers(self) -> "EBMCNNConfig":
        for pairs in (self.kernel_sizes, self.strides):
            if len(pairs) != len(self.channels) or any(len(pair) != 2 for pair in pairs):
                raise ValueError("EBM kernels and strides require one pair per CNN layer")
        return self


class EBMModelIntent(StrictModel):
    architecture: Literal["ebm_cnn_v1"] = "ebm_cnn_v1"
    config: EBMCNNConfig = Field(default_factory=EBMCNNConfig)


class EBMHyperparameters(StrictModel):
    epochs: int = Field(default=1, ge=1, le=1000)
    batch_size: int = Field(default=32, ge=3, le=256)
    seed: int = Field(default=43, ge=0, le=2147483647)
    learning_rate: float = Field(default=0.001, ge=1e-8, le=0.1)
    weight_decay: float = Field(default=0.00001, ge=0, le=1)
    rank_group_size: int = Field(default=3, ge=2, le=4)
    pair_sample_size: int = Field(default=512, ge=1, le=16384)
    cdf_anchor_weight: float = Field(default=1.0, ge=0, le=100)
    cdf_mean_corr_weight: float = Field(default=1.0, ge=0, le=100)
    wafer_rank_weight: float = Field(default=1.0, ge=0, le=100)
    reconstruction_weight: float = Field(default=1.0, ge=0, le=100)
    cdf_anchor_eps: float = Field(default=0.01, ge=1e-6, le=0.1)
    wafer_rank_margin: float = Field(default=0.3, ge=0, le=10)
    risk_z: float = Field(default=1.645, ge=0, le=10)

    @model_validator(mode="after")
    def validate_objective(self) -> "EBMHyperparameters":
        try:
            EBMTrainingConfig.from_dict(self.model_dump())
        except MLError as error:
            raise ValueError(str(error)) from error
        return self


class EBMTrainingIntent(StrictModel):
    schema_version: Literal["1"] = "1"
    recipe: EBMRecipeRef
    dataset: VersionRef
    model: EBMModelIntent = Field(default_factory=EBMModelIntent)
    training: EBMHyperparameters = Field(default_factory=EBMHyperparameters)
    resource_profile_id: Literal["cpu-ebm-v1"] = "cpu-ebm-v1"


class SubmitRequest(StrictModel):
    spec: TrainingIntent | EBMTrainingIntent
    retry_of_job_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("spec", mode="before")
    @classmethod
    def select_workload(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("recipe"), dict):
            if value["recipe"].get("id") == "ebm_poc_replay_v1":
                return EBMTrainingIntent.model_validate(value)
        return value


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
