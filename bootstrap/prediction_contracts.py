"""Extract public contracts from verified bundles using the actual HTTP DTOs."""

import copy
import hashlib
import json
from typing import Any

from src.api.ebm_schemas import EBMPredictionRequest
from src.api.ml_schemas import PredictionRequest
from src.api.prediction_docs import PREDICT_EXAMPLES, GatewayPredictionResult
from src.domain.ml import SchemaMismatch, canonical_json
from src.domain.model_registry.models import ModelBinding
from src.domain.prediction.catalog import CONTRACT_VERSION, PublicModelContract
from src.infrastructure.serving.materializer import BundleMaterializer, verify_bundle


def extract_public_contract(
    binding: ModelBinding,
    materializer: BundleMaterializer,
    *,
    expected_code_digest: str | None = None,
) -> PublicModelContract:
    directory = materializer.materialize(binding.artifact_ref, binding.manifest_digest)
    manifest = verify_bundle(directory, binding.manifest_digest)
    entries = {entry["path"]: entry for entry in manifest["artifact"]["files"]}

    def read_member(name: str) -> dict[str, Any]:
        entry = entries.get(name)
        if entry is None:
            raise SchemaMismatch("The verified bundle lacks a public contract member")
        data = (directory / name).read_bytes()
        if len(data) > 65536 or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise SchemaMismatch("Public contract member failed verification")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise SchemaMismatch("Invalid public contract member")
        return value

    feature = read_member("feature_schema.json")
    output = read_member("output_mapping.json")
    preprocessing = read_member("preprocessing.json")
    contract = manifest["contract"]
    schema_version = contract["input_schema_version"]
    if schema_version == "1" and contract["output_schema_version"] == "1":
        from src.infrastructure.serving.pytorch_runtime import PyTorchRuntime
        from src.plugins.models.mlp import build_mlp

        runtime = PyTorchRuntime(
            directory, binding.manifest_digest, build_mlp, expected_code_digest=expected_code_digest
        )
        runtime.close()
        request_model = PredictionRequest
        kind = "mlp"
        output_type = "Prediction"
        semantics = {
            "score_kind": output["score_kind"],
            "labels": output["labels"],
            "calibrated": False,
        }
        units = {"sensor_a": None, "sensor_b": None}
        public_preprocessing = {
            "strategy": manifest["preprocessing"]["strategy"],
            "fit_split": manifest["preprocessing"]["fit_split"],
            "applied_by": "serving",
        }
    elif (
        schema_version == "ebm-spectrogram-input-v1"
        and contract["output_schema_version"] == "ebm-neural-output-v1"
    ):
        from src.infrastructure.serving.ebm_runtime import EBMRuntime
        from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn

        ebm_runtime = EBMRuntime(
            directory,
            binding.manifest_digest,
            build_ebm_cnn,
            expected_code_digest=expected_code_digest,
        )
        ebm_runtime.close()
        request_model = EBMPredictionRequest
        kind = "ebm"
        output_type = "EBMNeuralPrediction"
        semantics = {
            "score_kind": output["score_kind"],
            "sigma_kind": output["sigma_kind"],
            "risk_z": output["risk_z"],
            "latent_dim": output["latent_dim"],
            "calibrated": False,
        }
        units = {"features": None}
        public_preprocessing = {
            "strategy": preprocessing["strategy"],
            "fit_split": preprocessing["fit_split"],
            "applied_by": "serving",
            "spectrogram": read_member("spectrogram_config.json"),
            "caller_input": "Precomputed spectrogram with this configuration and signal/pad order.",
        }
    else:
        raise SchemaMismatch("Unsupported inference contract version")
    example = copy.deepcopy(PREDICT_EXAMPLES[kind]["value"])
    request_model.model_validate(example)
    response_schema = GatewayPredictionResult.model_json_schema()
    response_schema["properties"]["predictions"] = {
        "type": "array",
        "items": {"$ref": f"#/$defs/{output_type}"},
    }
    if kind == "ebm":
        latent_schema = response_schema["$defs"]["EBMNeuralPrediction"]["properties"]["latent"]
        latent_schema.update(minItems=output["latent_dim"], maxItems=output["latent_dim"])
    document = {
        "contract_version": CONTRACT_VERSION,
        "input_schema_version": schema_version,
        "output_schema_version": contract["output_schema_version"],
        "input_schema": request_model.model_json_schema(),
        "output_schema": response_schema,
        "request_example": example,
        "input_units": units,
        "limits": {"max_batch": 128 if kind == "mlp" else 16, "max_body_bytes": 8 * 1024 * 1024},
        "preprocessing": {**public_preprocessing, "feature_schema_version": feature["version"]},
        "output_semantics": semantics,
        "registry_backend": "mlflow" if binding.artifact_ref.startswith("runs:/") else "local",
    }
    return PublicModelContract(
        binding.binding_id, binding.manifest_digest, canonical_json(document)
    )
