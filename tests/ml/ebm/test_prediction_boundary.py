import threading

import numpy as np
import pytest
from pydantic import ValidationError

from src.api.ebm_schemas import EBMPredictionRequest
from src.application.prediction.service import EBMPredictionService
from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.ml import Actor, CapacityExceeded, Forbidden, MLError, NotFound
from src.domain.prediction.models import ServingDeployment
from src.infrastructure.serving.bounded_ebm_prediction import BoundedEBMPredictionExecutor
from src.infrastructure.serving.bounded_prediction import PredictionDeadline


def request_body():
    return {"schema_version": "ebm-spectrogram-input-v1", "endpoint_id": "ebm",
        "signal_order": list(EBM_SIGNAL_ORDER), "pad_order": list(EBM_PAD_ORDER),
        "instances": [{"features": np.zeros((7, 4, 9, 31), dtype=np.float32).tolist()}]}


def prediction():
    return {"mu": 0.0, "sigma": 1.0, "latent": [0.0] * 112, "raw_nn_score": 1.645,
            "score_kind": "uncalibrated_latent_mean_plus_std"}


@pytest.mark.parametrize("case", ["signal", "pad", "shape", "bool", "nan", "large", "version"])
def test_ebm_http_schema_rejects_ambiguous_or_invalid_features(case):
    body = request_body()
    if case == "signal":
        body["signal_order"].reverse()
    elif case == "pad":
        body["pad_order"].reverse()
    elif case == "shape":
        body["instances"][0]["features"][0][0][0].pop()
    elif case == "bool":
        body["instances"][0]["features"][0][0][0][0] = True
    elif case == "nan":
        body["instances"][0]["features"][0][0][0][0] = float("nan")
    elif case == "large":
        body["instances"] *= 17
    else:
        body["schema_version"] = "1"
    with pytest.raises(ValidationError):
        EBMPredictionRequest.model_validate(body)


def test_prediction_timeout_keeps_ebm_physical_capacity():
    started, release = threading.Event(), threading.Event()

    class BlockingRuntime:
        def predict(self, values, **kwargs):
            started.set()
            release.wait(timeout=3)
            return [prediction()]

        def close(self):
            pass

    executor = BoundedEBMPredictionExecutor(BlockingRuntime(), latent_dim=112,
                                           capacity=1, timeout=0.02)
    batch = EBMPredictionRequest.model_validate(request_body()).to_domain()
    try:
        with pytest.raises(PredictionDeadline):
            executor.predict(batch)
        assert started.is_set()
        with pytest.raises(CapacityExceeded):
            executor.predict(batch)
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize("case", ["count", "nan", "sigma", "latent", "bool", "classifier"])
def test_ebm_output_boundary_rejects_wrong_neural_contract(case):
    value = prediction()
    if case == "nan":
        value["mu"] = float("nan")
    elif case == "sigma":
        value["sigma"] = -1
    elif case == "latent":
        value["latent"] = [0.0]
    elif case == "bool":
        value["raw_nn_score"] = True
    elif case == "classifier":
        value = {"class_id": 1, "label": "positive", "score": 0.8}

    class WrongRuntime:
        def predict(self, values, **kwargs):
            return [] if case == "count" else [value]

        def close(self):
            pass

    executor = BoundedEBMPredictionExecutor(WrongRuntime(), latent_dim=112)
    try:
        batch = EBMPredictionRequest.model_validate(request_body()).to_domain()
        with pytest.raises(MLError):
            executor.predict(batch)
    finally:
        executor.close()


def test_ebm_prediction_service_checks_scope_before_computation():
    class NeverCompute:
        def predict(self, values):
            pytest.fail("Out-of-scope prediction must not start model computation")

    deployment = ServingDeployment("tenant", "ebm", "deployment", 1, "model", "v1")
    service = EBMPredictionService(NeverCompute(), deployment)
    batch = EBMPredictionRequest.model_validate(request_body()).to_domain()
    with pytest.raises(Forbidden):
        service.predict(Actor("tenant", "user", frozenset()), "ebm", batch)
    with pytest.raises(NotFound):
        service.predict(Actor("other", "user", frozenset({"ml:predict"})), "ebm", batch)
    with pytest.raises(NotFound):
        service.predict(Actor("tenant", "user", frozenset({"ml:predict"})), "other", batch)
