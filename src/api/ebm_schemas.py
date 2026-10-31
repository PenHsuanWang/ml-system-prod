"""Bounded HTTP schema for the approved, precomputed EBM feature contract."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from src.api.ml_schemas import StrictModel
from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.prediction.ebm import EBMPredictionBatch

FiniteValue = Annotated[float, Field(allow_inf_nan=False)]
TimeBins = Annotated[list[FiniteValue], Field(min_length=31, max_length=31)]
FrequencyBins = Annotated[list[TimeBins], Field(min_length=9, max_length=9)]
PadFeatures = Annotated[list[FrequencyBins], Field(min_length=4, max_length=4)]
SignalFeatures = Annotated[list[PadFeatures], Field(min_length=7, max_length=7)]


class EBMInstance(StrictModel):
    features: SignalFeatures


class EBMPredictionRequest(StrictModel):
    schema_version: Literal["ebm-spectrogram-input-v1"]
    endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    signal_order: list[str] = Field(min_length=7, max_length=7)
    pad_order: list[str] = Field(min_length=4, max_length=4)
    instances: list[EBMInstance] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_orders(self) -> "EBMPredictionRequest":
        if tuple(self.signal_order) != EBM_SIGNAL_ORDER or tuple(self.pad_order) != EBM_PAD_ORDER:
            raise ValueError("Unapproved EBM signal or Pad order")
        return self

    def to_domain(self) -> EBMPredictionBatch:
        values = tuple(tuple(tuple(tuple(tuple(time) for time in frequency)
                     for frequency in pads) for pads in instance.features)
                     for instance in self.instances)
        return EBMPredictionBatch(values, tuple(self.signal_order), tuple(self.pad_order))
