"""Public HTTP descriptions and wire shapes for the prediction gateway."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.datasets.ebm import EBM_FEATURE_SHAPE, EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.prediction.ebm import EBMNeuralPrediction
from src.domain.prediction.models import Prediction

GATEWAY_DESCRIPTION = """
Run inference using a logical endpoint assigned to your tenant.

1. Obtain your endpoint ID, its input contract, and the Management URL from the deployment owner.
   The reference Compose Management URL uses port 8000.
2. Sign in at Management `POST /api/v1/identity/login` with `tenant_id`, `username`, and `password`.
   Your account needs `ml:predict`. Paste the returned `access_token` into **Authorize**.
3. Open **Predict with an active endpoint**, choose **Try it out**, replace the synthetic endpoint
   in an example with your assigned endpoint, and supply your data.
4. Record `request_id` and `provenance` to identify the model version used for that request.

Synthetic endpoint IDs illustrate request structure. Ask the deployment owner for a deployed ID.
Endpoint discovery is planned. Users with `ml:model:read` can inspect platform-registered versions
through Management `GET /api/v1/model_registry/models` and
`GET /api/v1/model_registry/models/{name}/versions`.
A registry listing describes registered bindings and requires the deployed endpoint mapping
before prediction. The Gateway derives tenant identity from the authenticated session.
"""

PREDICT_DESCRIPTION = f"""
Requires a current Bearer session and `ml:predict`. Resolves `endpoint_id` within your tenant.
Model updates and rollback can change the version behind that endpoint between requests.
The Gateway checks runtime readiness, response provenance, and the route again before replying.

**MLP input** uses `schema_version="1"`, 1–128 instances, and finite numeric `sensor_a` and
`sensor_b` values in [-100, 100]. Each result contains `class_id`, `label`, `score`, and
`score_kind="uncalibrated_softmax"`. The score is an uncalibrated classification score.

**EBM input** uses `schema_version="ebm-spectrogram-input-v1"` and 1–16 precomputed spectrograms.
Each instance has shape [7, 4, 9, 31], ordered as signal, pad, frequency, time.
The required signal order is `{list(EBM_SIGNAL_ORDER)!r}` and pad order is
`{list(EBM_PAD_ORDER)!r}`. Supply finite numeric values using the deployed preprocessing contract.
Results contain `mu`, `sigma`, `latent`, `raw_nn_score`, and
`score_kind="uncalibrated_latent_mean_plus_std"`.
Here `sigma` describes latent-feature standard deviation. The score is uncalibrated.

The body limit is 8 MiB. Unknown fields and invalid shapes are rejected with 422.
A successful response includes the actual registered model/version, deployment ID, generation,
input schema version, and immutable specification/manifest digests in `provenance`.
With an MLflow backend, the registered name/version identifies the bound MLflow model version.

Send a valid `X-Request-ID` for correlation, or use the generated response header.
Gateway-generated domain errors echo it in their body. Validation errors (422) currently generate
a separate body request ID; correlate those using the response header. Proxied responses keep the
upstream body request ID. The Gateway forwards its request ID to Serving.

For 401, sign in again. For 403, request the required permission. For 404, confirm your endpoint.
For 413/422, correct the payload. For 429/503/504, use bounded backoff and contact the owner if
failures persist. A forwarded Serving deadline can produce 504; an HTTP transport timeout produces
503. An unparseable upstream error also produces 503.
Prediction has no idempotency-key or replay contract. A retry can execute inference again and can
use a newer generation. Keep the provenance returned by each successful request.
"""

_signals, _pads, _frequencies, _times = EBM_FEATURE_SHAPE
MLP_EXAMPLE = {
    "summary": "Synthetic MLP input; replace the endpoint with your assigned ID",
    "value": {
        "endpoint_id": "synthetic-quality",
        "schema_version": "1",
        "instances": [{"features": {"sensor_a": 0.2, "sensor_b": -0.1}}],
    },
}


PREDICT_EXAMPLES = {
    "mlp": MLP_EXAMPLE,
    "ebm": {
        "summary": "Synthetic EBM spectrogram; replace the endpoint and feature values",
        "value": {
            "endpoint_id": "synthetic-ebm",
            "schema_version": "ebm-spectrogram-input-v1",
            "signal_order": list(EBM_SIGNAL_ORDER),
            "pad_order": list(EBM_PAD_ORDER),
            "instances": [{"features": [[[[0.0] * _times] * _frequencies] * _pads] * _signals}],
        },
    },
}


class PredictionProvenance(BaseModel):
    endpoint_id: str = Field(description="Logical endpoint used for this request.")
    deployment_id: str = Field(description="Immutable deployment selected by the route.")
    generation: int = Field(description="Deployment generation; rollback creates a new generation.")
    registered_model: str = Field(description="Registered model name bound to the deployment.")
    model_version: str = Field(description="Fixed registered version bound to this deployment.")
    input_schema_version: str = Field(description="Input contract used by the Serving runtime.")
    spec_digest: str = Field(description="Digest of the immutable deployment specification.")
    manifest_digest: str = Field(description="Digest of the verified model bundle manifest.")
    pod_uid: str = Field(
        description="Serving Pod UID on Kubernetes; an empty string for local processes."
    )


class GatewayPredictionResult(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "predictions": [
                        {
                            "class_id": 1,
                            "label": "positive",
                            "score": 0.73,
                            "score_kind": "uncalibrated_softmax",
                        }
                    ],
                    "provenance": {
                        "endpoint_id": "synthetic-quality",
                        "deployment_id": "synthetic-deployment",
                        "generation": 3,
                        "registered_model": "synthetic-mlp",
                        "model_version": "7",
                        "input_schema_version": "1",
                        "manifest_digest": "a" * 64,
                        "spec_digest": "b" * 64,
                        "pod_uid": "",
                    },
                    "request_id": "synthetic-request",
                }
            ]
        }
    )
    predictions: list[Prediction] | list[EBMNeuralPrediction] = Field(
        description="One result per input, in input order; MLP classification or EBM neural output."
    )
    provenance: PredictionProvenance = Field(description="Identity of the actual serving model.")
    request_id: str = Field(description="Request identifier for support and tracing.")


class PayloadTooLarge(BaseModel):
    code: Literal["PAYLOAD_TOO_LARGE"]
    message: str = Field(description="Sanitized explanation of the failure.")
    retryable: bool = Field(description="Whether a bounded retry may be appropriate.")


class PredictionError(BaseModel):
    code: str = Field(description="Stable machine-readable domain error code.")
    message: str = Field(description="Sanitized explanation of the failure.")
    request_id: str = Field(description="Request identifier for support and tracing.")
    retryable: bool = Field(description="Whether a bounded retry may be appropriate.")


PREDICTION_ERRORS = {
    code: {"description": description, "model": PredictionError}
    for code, description in (
        (401, "Missing, invalid, expired, or revoked Bearer session. Sign in again."),
        (403, "The authenticated account lacks ml:predict."),
        (404, "The endpoint or deployment is unavailable to this tenant."),
        (422, "Invalid fields, schema version, value range, batch size, or signal/pad order."),
        (429, "Serving capacity is occupied. Retry with bounded backoff."),
        (503, "Identity, SQL route, runtime identity, or HTTP transport is unavailable."),
        (504, "Serving returned PREDICTION_DEADLINE. Retry with bounded backoff."),
    )
}
PREDICTION_ERRORS[413] = {
    "description": "Body exceeds 8 MiB. Reduce the batch. Use the request ID header.",
    "model": PayloadTooLarge,
}
