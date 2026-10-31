# EBM 實作與使用說明

更新日期：2026-10-31。本文描述 `ebm_poc_replay_v1@1.0.0` 的本機工程能力；原始整合計畫的工作包與數值／環境驗收條件仍適用。

## 已實作的流程

```text
租戶 FeatureSnapshot → 受控 EBM Recipe → Durable Job / Outbox
  → CPU Training Worker → 凍結 Neural Bundle → Registry Binding
  → 同一 Bundle 的 EBM 評估 → 明確品質政策 → 人工核准
  → 固定版本 Deployment → 預先計算 Spectrogram 的 NN Serving
```

MLP 使用原有二分類契約。EBM 的模型、資料、Evaluator、Runtime 及輸入／輸出版本由 Recipe 配對，請求不能指定 Python import path 或替換政策。共用 Supervisor 的成功及 ERROR 紀錄使用正確的 workload provenance。

本輪修正了不相容 Resume 改寫 `training_spec.json` 的問題。Checkpoint 驗證、模型／Optimizer／RNG 恢復成功後才發布新輸出；同目錄拒絕續跑時，原始 Spec、Checkpoint 與 Metrics 保持原始 bytes。Bundle 內同時保存平台 Job 的 `training_spec_digest` 與內部續跑用的 `trainer_spec_digest`。

EBM Runtime 強制核對可信 Code Digest、Torch CPU 相容性、固定訊號／Pad 順序、Spectrogram 參數、Normalizer 與輸出維度。NPZ 在配置陣列前檢查 NPY Header、Shape、Dtype 和 Payload 大小，且禁止 pickle。Runtime 使用驗證後的同一份 bytes 建立模型，避免驗證與讀取之間的檔案替換。

## 本機執行

使用 Python 3.11／3.12，並依 README 建立隔離環境：

```sh
python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e '.[dev,training,ebm]'

# 獨立 Feature → Trainer → Bundle → 精確 Bundle 評估
python -m bootstrap.ebm_cli smoke --output var/ebm-smoke

# 平台 Dataset Catalog → Job → 子程序 → Registry → 評估
python -m bootstrap.ml_cli demo-ebm
```

第二個指令明確執行 migration，使用合成資料與開發用本機 Registry。它不會自動核准或部署模型。未設定品質政策時，Job 可成功完成，但 Evaluation 為 `REJECTED_BY_POLICY`，且不能取得 Approval。

獨立載入 Bundle 不需要 Spark 或原始訓練資料：

```sh
python -m bootstrap.ebm_cli verify \
  --bundle /absolute/path/to/bundle \
  --manifest-digest <result.json中的manifest_digest>
```

此獨立 CLI 的 Code Digest 由本機來源檔案計算；平台 Worker／Serving 則使用凍結的 `ML_CODE_REVISION`。兩條執行路徑的來源綁定必須一致，不能任意互換其預期 Digest。

Epoch Checkpoint 可透過 `bootstrap.ebm_cli train --resume` 在相同設定下恢復。本輪平台支援命令重送與評估階段恢復而不重訓；尚未提供跨 Job／Attempt 自動選取訓練 Checkpoint 的功能。

## 匯入資料與提交工作

先在授權的上游流程產生 Metadata JSON 與 NPZ，再明確匯入：

```sh
python -m bootstrap.ml_cli register-ebm-dataset \
  --tenant-id demo --dataset-id fdc --version v1 \
  --metadata-path /path/to/metadata.json --npz-path /path/to/spectrograms.npz
```

NPZ 只能包含 `spectrograms`：有限 `float32 [N,7,4,9,31]`。Metadata 包含：

- `schema_version: ebm-feature-snapshot-v1`、固定 `signal_order`／`pad_order`。
- `feature_config: {"version":"fdc_spectrogram_v1","log1p":true}`。
- 每筆 wafer row 的 `sample_id`、`lot_id`、`recipe_id`、`segment_id`、`wafer_id`、`event_time`、`available_at`、`cdf_target`，以及啟用 wafer rank 時的四個 `pad_counts`。
- 非空、互斥且完整覆蓋的 `train`／`validation`／`test` indices。同 Lot 不得跨 split，且同 Lot 的多筆 wafer rows 必須使用相同特徵。

時間欄位必須包含時區；`available_at: null` 明確表示未知，這類資料只適用於回溯重播。完整範例見 `tests/ml/ebm/test_platform_training.py`。資料發布採內容定址，既有 tenant／ID／version 不可替換。

透過既有 `POST /api/v1/training/jobs`，帶入 Bearer Token 與 `Idempotency-Key`：

```json
{
  "spec": {
    "recipe": {"id": "ebm_poc_replay_v1", "version": "1.0.0"},
    "dataset": {"id": "fdc", "version": "v1"},
    "training": {"epochs": 1, "batch_size": 32}
  }
}
```

EBM 預設使用 `ebm_cnn_v1`、`cpu-ebm-v1` 及 Notebook 的啟用中 Loss 組合。特徵建構器接收已在上游取樣為 1 Hz 的 traces；資料來源查詢、時間插值及原始 CDF targets 建立仍由上游負責。

## 品質政策與固定 Serving

管理／提交端的 `ML_EBM_QUALITY_POLICY_JSON` 可設定經模型／產品方審核的 JSON，必須完整包含 `policy_version`、`max_anchor_loss`、`minimum_lot_correlation`、`minimum_lots`。本專案不替真實資料提供預設品質門檻；`ebm-unqualified-v1` 是保留的未核定狀態。

政策內容與版本會一起凍結到 Job Spec，後續修改環境變數不會改寫已接受的工作。評估計算 latent CDF anchor loss 與 Lot 平均 raw score/CDF Pearson；沒有匹配政策或 correlation 無法定義時，不能核准。通過政策後仍須使用既有 Approval API，再建立 Deployment。

EBM Serving 使用相同 prediction endpoint，輸入契約為：

```text
schema_version = "ebm-spectrogram-input-v1"
endpoint_id = 已部署的 endpoint
signal_order = Metadata 中的七個固定訊號
pad_order = ["P1", "P2", "P3", "P4"]
instances = [{features: [7,4,9,31] 的未標準化 Spectrogram}, ...]
```

每次最多 16 個 instances，Ingress／Serving 的 body 上限為 8 MiB；Management training API 維持 64 KiB。Serving 套用 Bundle 裡凍結的 Normalizer，輸出 `mu`、`sigma`、`latent`、`raw_nn_score`，以及明確的 `score_kind`。`sigma` 是 latent feature 標準差，輸出不包含已校正 Energy 或 RUL。逾時不會提早歸還仍在執行的物理運算容量。

## 驗證與未完成範圍

```sh
python scripts/check_boundaries.py
python scripts/check_ebm_typing.py
python -m pytest tests/ml tests/test_boundaries.py -q
python scripts/verify_ml_acceptance.py
```

本機 smoke 使用受限主機上的 Python 3.10、cached Torch 2.0.1、NumPy 1.26.4／SciPy 1.15.3；ASGI 回歸沿用已有的 test-only selector workaround。這些證據不構成 Python 3.12／Torch 2.6.0、真實 MLflow／RabbitMQ／PostgreSQL 或容器的正式資格驗證。

2026-10-31 本輪驗證：ML／boundary suite 共 **468 passed、3 skipped、0 failures／errors**；核心與 EBM strict typing、Pyflakes、依賴邊界、編譯均通過。三個跳過項目需要真實管理／預測服務及 RabbitMQ／Redis。另嘗試的舊模板 module／distributed suite 因本機 Python 3.10 缺少 `datetime.UTC`／`enum.StrEnum`、且未安裝 Taskiq 而無法收集，不算作通過。可追溯摘要見 [本輪證據](evidence/ebm-2026-10-31/result.json)。

| 工作包 | 現況 | 尚待完成／驗證 |
| --- | --- | --- |
| WP-01 | Notebook Hash、Replay 語意及合成 Fixture 已具備 | 真實脫敏 Fixture 與 Notebook Reference Outputs |
| WP-02 | 本機不可變 Snapshot、tenant Catalog、受控 Recipe 已實作 | 來源完整 provenance 及真實資料規模資格 |
| WP-03 | 特徵、CNN、Loss、Sampler、CPU Trainer、Epoch Resume 已實作 | 真實 PoC 數值對照、目標環境與資源量測 |
| WP-04 | Neural Bundle、獨立 Runtime、Golden I/O、Evaluator／Policy 接點已實作 | 真實品質門檻與目標 Runtime 資格 |
| WP-05 | 本機 Job／Registry／Approval／Deployment／API 已接入 | 真 MLflow round trip、多角色服務與目標環境驗收 |
| WP-06 | 未實作 | Databricks ingestion／Export 或 Remote Executor |
| WP-07 | 未實作 | CalibrationSnapshot、Batch Analysis、RUL、Report Store |
| WP-08 | 未實作 | As-of 時序限制與獨立 Rolling-origin 驗證 |

本次新增測試與來源檔案需與修改一起納入版本控制；本輪不自動建立 Commit。原始 Notebook 的內容保持不變。
