"""Public HTTP descriptions and wire shapes for the prediction gateway."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.datasets.ebm import EBM_FEATURE_SHAPE, EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.prediction.ebm import EBMNeuralPrediction
from src.domain.prediction.models import Prediction

GATEWAY_DESCRIPTION = """
此入口對指定的邏輯端點執行預測。從這裡開始：

1. 向部署負責人取得你的 `endpoint_id`、輸入規格及 Management 的網址。參考 Compose 使用 port 8000。
2. 在 Management 的 `POST /api/v1/identity/login` 以 `tenant_id`、`username`、`password`
   登入。帳號需要 `ml:predict`。複製回傳的 `access_token`，在本頁 **Authorize** 貼入 token。
3. 展開 **執行端點預測**，按 **Try it out**，將範例端點換成你的端點並填入資料。
4. 成功後以 `provenance.registered_model`、`model_version` 和 `generation` 核對這次實際使用的模型。

目前 Gateway 沒有端點清單。若有 `ml:model:read`，可在 Management 查詢
`GET /api/v1/model_registry/models` 和 `GET /api/v1/model_registry/models/{name}/versions`。
這些是平台為租戶登錄的版本。要呼叫預測，仍須取得已啟用的端點 ID。
"""

PREDICT_DESCRIPTION = f"""
依登入租戶和 `endpoint_id` 選擇當下的有效部署。相同端點在更新或回滾後可能使用不同模型版本。
每個請求上限為 8 MiB。

* **MLP** 使用 `schema_version="1"`。每次 1–128 筆，每筆包含 `sensor_a`、`sensor_b`，
  各為 −100 到 100 的有限數值。回傳類別、標籤與未校準的 softmax 分數。
* **EBM** 使用 `schema_version="ebm-spectrogram-input-v1"`。每次 1–16 筆已計算的頻譜。
  每筆形狀為 `[7,4,9,31]`（signal、pad、frequency、time），依下方順序排列。
  回傳 `mu`、`sigma`、`latent` 和未校準的 `raw_nn_score`。

以下 Python 產生一筆格式有效的合成 EBM 請求。將端點與頻譜換成部署提供的值：

```python
spectrum = [[[[0.0] * 31 for _ in range(9)] for _ in range(4)] for _ in range(7)]
ebm_body = {{
    "endpoint_id": "synthetic-ebm",
    "schema_version": "ebm-spectrogram-input-v1",
    "signal_order": {list(EBM_SIGNAL_ORDER)!r},
    "pad_order": {list(EBM_PAD_ORDER)!r},
    "instances": [{{"features": spectrum}}],
}}
```

模型註冊名稱與固定版本記錄於成功回應的 `provenance`。使用 MLflow 時，
這是該部署綁定的 MLflow 註冊版本。`request_id` 可供維運人員追查。
"""

_signals, _pads, _frequencies, _times = EBM_FEATURE_SHAPE
MLP_EXAMPLE = {
    "summary": "MLP 合成範例，請換成你的端點",
    "value": {
        "endpoint_id": "synthetic-quality",
        "schema_version": "1",
        "instances": [{"features": {"sensor_a": 0.2, "sensor_b": -0.1}}],
    },
}


PREDICT_EXAMPLES = {
    "mlp": MLP_EXAMPLE,
    "ebm": {
        "summary": "EBM 單筆合成頻譜，請換成你的端點與實際頻譜",
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
    endpoint_id: str
    deployment_id: str
    generation: int = Field(description="實際服務請求的部署世代，回滾也會建立新世代。")
    registered_model: str
    model_version: str = Field(description="該次部署所綁定的固定註冊版本。")
    input_schema_version: str
    spec_digest: str
    manifest_digest: str
    pod_uid: str = Field(description="Kubernetes Serving Pod 的 UID，本機 Serving 為空字串。")


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
    predictions: list[Prediction] | list[EBMNeuralPrediction]
    provenance: PredictionProvenance
    request_id: str


class PayloadTooLarge(BaseModel):
    code: Literal["PAYLOAD_TOO_LARGE"]
    message: str
    retryable: bool


class PredictionError(BaseModel):
    code: str
    message: str
    request_id: str
    retryable: bool


PREDICTION_ERRORS = {
    code: {"description": description, "model": PredictionError}
    for code, description in (
        (401, "請登入或更新已失效的 Bearer token。"),
        (403, "請確認帳號具備 ml:predict。"),
        (404, "此租戶找不到端點，請確認部署提供的 endpoint_id。"),
        (422, "請核對 schema_version、欄位、數值範圍、批次大小或頻譜順序。"),
        (429, "Serving 已達容量限制，請稍後退避重試。"),
        (503, "相依服務、有效路由或實例尚不可用，或預測期間路由已變更。"),
        (504, "預測逾時，請以 request_id 追查。"),
    )
}
PREDICTION_ERRORS[413] = {"description": "請縮小批次至 8 MiB 內。", "model": PayloadTooLarge}
