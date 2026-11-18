# EBM 後續工作與驗收邊界

更新：2026-11-19。原始 Neural、DatasetBuilder、Job／Registry／Approval、固定
Serving 與完整 Compose／K8s 工程生命週期已交付；操作集中於
[EBM 使用說明](ebm-implementation-status.md)，現行證據集中於
[實作狀態](implementation-status.md)。本頁只保留尚未完成的工作與不可放寬的契約。

## 已實作與待驗收

| 範圍 | 已有能力 | 仍需要的工作／證據 |
| --- | --- | --- |
| Inputs／features | `ebm-raw-input-v1`、operator DatasetBuilder、immutable receipt、tenant DatasetVersion、train-only Normalizer | 企業 schema mapping、合法脫敏資料、來源品質與真實規模資格 |
| Neural | Spectrogram、CNN、Loss／sampler、CPU trainer、epoch resume、Bundle／Golden I/O | 固定資料、設定、版本及裝置的 Notebook 數值 parity；正式資源預算 |
| Governance／runtime | 精確 Bundle 評估、明確 Approval、MLflow／S3、固定 Serving、更新／rollback、Pod 故障恢復 | 代表性品質門檻、人工品質核准及目標環境資格 |
| Databricks | OAuth M2M、固定 Delta SQL batch、Raw Snapshot | 真實 Warehouse fixture；自動 ingestion-to-build、Export orchestration 與 Remote Executor 未實作 |
| Calibration／analysis | 未實作 | CalibrationSnapshot、Batch Analysis／Report Store、calibrated Energy／RUL |
| Temporal | 未實作 | as-of 可得時間、rolling-origin、晚到標籤與校正切換驗證 |

合成 fixture 與綠色 CI 不能代替企業資料品質或 PoC 數值對照。
原始 Notebook 的來源 hash、觀察與差距保留於 [原始 fit–gap](EBM_PoC_ML_System_Fit_Gap_2026-10-29.md)。

## 兩條演算法軌道

- 已註冊的 `ebm_poc_replay_v1` 用於工程重播；不得把回溯重播的分數當成未來預測品質。
- `ebm_temporal_v1` 仍是提案。訓練期固定 CDF reference，依 `event_time` 與
  `available_at` 切分訓練／校正／測試，RUL anchor 只能來自當時可得的歷史。
- 修改 CDF、feedback、Aux Head、Fold、sampling 或 RUL 語意，必須建立新
  Recipe／feature／model 版本及基準，不能要求修改版仍重現舊結果。
- `mu`、`sigma`、`latent`、`raw_nn_score` 是目前 neural 契約；`sigma` 是 latent
  features 標準差，不是已校正機率或信賴區間。Energy、RUL 與 Pad decision 各有獨立契約。

## Calibration、報告與組合治理

CalibrationSnapshot 必須與模型分開版本化，至少固定：

1. model／feature version、適用機台／Recipe／Pad、來源 snapshot digests。
2. selected features、方向、權重、ECDF mappings、median bias、binning 與例外規則。
3. `as_of_time`、來源 `available_at` 上界、有效期間，以及 RUL anchor 的觀測／可得時間。
4. calibration／decision policy version 與 digest；替換校正不能改寫既有模型或報告。

Prediction／Report provenance 應固定 model + feature + calibration + decision
policy + input snapshot + as-of time。缺少歷史、過時校正或 CDF 必須回明確不可用，
不能偷看完整未來窗口。Batch Analysis 有自己的持久 ID、狀態與完整發布的 Report
Artifact，不偽造 Training 的 model binding／evaluation 終態。

Neural 模型 Approval 不涵蓋新 calibration／decision policy。Energy／RUL 評估與
核准須綁定完整組合的版本／digests；替換任一成員不能繼承不適用的既有核准。

## Remote Executor 的獨立契約

Snapshot Export → container PyTorch 與 Databricks Remote Executor 是不同選擇。
必須先確認資料移出政策、資料量與搬移成本；remote 不自動解決 `.toPandas()` 或
整批 Tensor 的記憶體問題。僅替換 `launch()` 不構成遠端執行支援。

Remote 至少需要固定執行定義、持久 submission key／run ID、owner／spec fencing、
queued／running／cancel／UNKNOWN、結果下載與對帳。提交成功但回應遺失、run ID
保存前崩潰、重送與 takeover、取消／完成競爭、遠端不可查、artifact 下載失敗都要
有實測；不能因短期查不到就重複 fit。外部 submitted 命令的 ACK 契約須另行定義，
目前 Worker 仍以 SQL 終態與 technical snapshot 決定 ACK。

## 交付順序與退出條件

1. 模型／資料方提供固定合法 fixture、原始設定及 reference outputs；逐項區分
   feature、NN、Energy、RUL，缺少 reference 的部分維持 pending。
2. 核准來源與企業資料 mapping；量測真實資料量的記憶體、時間與儲存需求。
3. 明確決定 export 或 remote；後者先完成可靠性契約再接入治理生命週期。
4. 實作 calibration／report 的獨立版本化及完成語意，再做 Temporal／as-of 評估。
5. 在獨立時間區間驗證晚到資料、校正切換、rollback 與單位／輸出契約，最後由
   模型／產品方提供品質門檻及人工核准。CPU／GPU 與套件版本變更需各自相容性資格。

每份結果記錄 source、image、資料／模型／receipt digests、容差與實際執行範圍。
MLP、現有 EBM、Workload Registry、資料格式、停止確認、Approval 與 route fencing
保留回歸；不以新計畫改寫先前的驗收結果。
