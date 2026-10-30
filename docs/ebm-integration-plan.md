# EBM Notebook 整合實作與驗收計畫

> 日期：2026-10-29；狀態：Draft / Proposed。本文定義後續開發工作，**不代表 EBM 元件已實作、訓練已執行或 Serving 已通過驗收**。  
> 依據：[Fit–Gap 分析](EBM_PoC_ML_System_Fit_Gap_2026-10-29.md)、[目前 LLD v1.3](pytorch-ml-system-lld.md)、[ADR-014](adr/014-implementation-baseline.md)、[實作狀態](implementation-status.md)、[原始 Notebook](../EBM_final_forline.ipynb)。  
> 範圍：保留現有控制與治理能力，新增版本化 EBM workload。下列名稱、schema 與工作包為提案，尚未註冊新的正式 ML-SPEC 編號。

審查時程式基線為 `a88f6ff6d1c19987c7cbf4b1f90280d19f83786a`；Notebook SHA-256 為 `6559bcd463a79b4b9697a7d3916e110851d02a05ea3bbbb8e96d4d9f0448fba9`。Notebook 以工作目錄檔案為依據，未假定它已包含於該 commit。

## 1. 交付目標與既有基礎

第一個工程交付目標是：固定的小型 FDC／CDF／Pad 資料 → Spectrogram → CPU EBM 訓練 → 完整模型 Bundle → 乾淨程序載入與 Golden I/O → EBM 專用評估。之後接入既有 Job／Registry／Approval／Deployment，完成固定版本批次報告及純 NN Serving。

接入平台時沿用 [ADR-014](adr/014-implementation-baseline.md) 與 [Supervisor.finalize](../src/application/training/supervisor.py) 的順序：凍結 Bundle → 註冊並建立外部 model binding → 評估同一份凍結 Bundle → 持久保存評估與 Training 終態 → 獨立人工核准 → 部署。訓練成功與品質政策通過不同；`REJECTED_BY_POLICY` 的有效評估可結束訓練，但不能用於模型核准。

目前已存在 durable Job、Attempt、Outbox、owner fencing、取消、CPU MLP 子程序、不可變 tenant dataset、MLflow Adapter、精確 Bundle 評估和固定版本 Serving。它們的程式與既有測試是回歸基礎；本計畫不把它們重新列成尚未開發的功能。遠端服務與目標 Runtime 的資格仍以實測證據為準。

遵循 [Constitution 的分層方向](../specs/constitution.md)：Domain／Application 使用純 DTO 與窄 Port；Torch、Spark、MLflow 留在 Plugin／Infrastructure；Bootstrap 組裝批准的實作。Management 不執行模型計算，通用 `server.py` 的預設入口維持中性。

## 2. 兩條獨立的演算法版本軌道

| 軌道 | 目的 | 初始語意 | 驗收依據 |
| --- | --- | --- | --- |
| PoC Replay（提案 key：`ebm_poc_replay_v1`） | 證明平台能忠實承載 Notebook | 保留固定訓練 Fold、目前無效的 feedback 路徑、關閉 Aux Head、原 CDF 與離線分析語意 | 同一固定資料、設定、版本與裝置的特徵／輸出／Loss 對照；標示為回溯重播 |
| Temporal Model（提案 key：`ebm_temporal_v1`） | 驗證對未來資料的預測能力 | 訓練期凍結 CDF reference、依可得時間切分資料與校正、RUL anchor 僅取當時歷史 | 獨立時間區間及 rolling-origin backtest；品質門檻由模型／產品方定義 |

工程重播通過不構成預測品質或線上 RUL 核准。修正 CDF、feedback、Aux Head、Fold 或 RUL 邏輯，都應更新演算法／Recipe 版本與基準；修正版不要求重現受舊語意影響的相同分數。

純 NN 的 `mu`、`sigma`、`latent`、`raw_nn_score`，校正後的 `calibrated_energy`，以及 RUL／Pad decision，採不同輸出契約。`sigma` 在目前模型中是 latent features 的標準差；不標示成已校正的機率或信賴區間。

## 3. 版本化契約

### 3.1 受控的 Workload 選擇

由批准的 Recipe 決定整組契約：

```text
recipe_id + recipe_version
  -> dataset schema + feature builder version
  -> model factory + trainer + loss/sampler configuration
  -> evaluator version + evaluation policy version
  -> artifact contract + serving runtime + input/output schema
```

解析後的 TrainingSpec 固定所有版本、資料 digest、程式 digest、映像與資源 profile；未知或不相容的組合在接受工作前拒絕。外部請求不能指定 import path 或自行替換 evaluator。MLP 的既有請求與模型版本仍解析到原本的二分類組合。

新增 CPU EBM resource profile 前先測量小型 fixture 的時間與記憶體。現有 [300 秒預設訓練 timeout](../bootstrap/ml_config.py) 不應直接當成真實 FDC 工作的核定預算；GPU 另作資格驗證。

### 3.2 資料快照與特徵

RawSnapshot 至少保存：

- 每張 LOT／FDC 來源表的識別、固定 Delta version 或不可變匯出內容 digest。
- 全部 CDF CSV 及 `p1_only.csv`～`p4_only.csv` 的內容 digest、schema、適用範圍。
- Recipe／Step 過濾設定、時區、時間窗、訊號與 Pad 順序、資料產生／取得時間。
- tenant、snapshot ID、父來源、發布狀態及內容 manifest digest。只有完整發布的 snapshot 可以解析。

每次工作讀取同一個固定版本；不同工作不得覆寫彼此快照。僅記錄表名、`READY` 或讀取前驗證 signature，無法保證之後讀到相同資料。

FeatureSnapshot 的契約包含 `[N, 7, 4, 9, 31]`、`float32`、有限值、Lot／Recipe key 與明確的樣本映射。固定 256 秒、Hann window、NFFT 16、overlap 8、detrend／scaling／magnitude／log1p、插值與 padding 規則都納入 feature version。Normalizer 僅對訓練 Lot 擬合，保留其統計量及訓練集合 digest。

Notebook 將一筆 Lot spectrogram 對應到多筆 wafer labels。資料契約須分開 `lot_features` 與 `wafer_targets`，保留重複權重與 Recipe／Four-Pad group；同 Lot 在 `eval()` 下得到相同 NN 特徵，不以多筆報告列數宣稱 wafer 級差異辨識。若產品需要 wafer-specific prediction，另定義特徵與模型版本。

Temporal 軌道先固定訓練截止時間，再擬合 CDF reference distribution；新增測試／未來資料不得改變既有訓練 targets。`event_time` 與 `available_at` 分開記錄；缺少可得時間證據時，只能完成離線重播驗收。

### 3.3 模型、校正與報告的版本

NN Bundle 至少包含模型建構參數、`state_dict`、訊號／Pad／輸入 shape、完整 Spectrogram config、凍結 Normalizer、輸出 mapping、Golden I/O、每個檔案 digest、可信 code／runtime binding。可沿用 Manifest v2 的治理結構，但必須擴充目前固定 MLP 的 verifier 與 loader；本文不是可直接載入的 Manifest 範例。

Serving 從 Bundle 讀取 Normalizer，不重新取得訓練資料擬合。訓練 provenance 和執行相容性分開：CPU／GPU 或 Torch 版本切換需有明確允許的 compatibility profile 與 Golden 測試，不能用放寬 fingerprint 檢查代替驗證。

訓練 resume checkpoint 另存 optimizer、epoch／batch 位置、Torch／NumPy／Python RNG、採樣狀態、設定與 dataset digest，必要時包含 scheduler。完成後的 Serving Bundle 是不可變成果；Notebook 訓練結束才存的權重檔不能直接當成中斷續跑契約。

CalibrationSnapshot 與模型分開版本化，至少記錄：

- 對應 model／feature version、適用機台／Recipe／Pad、來源 snapshot digest。
- selected features、方向、權重、ECDF mappings、median bias、binning 與例外規則。
- `as_of_time`、來源資料 `available_at` 上界、有效期間；RUL 用到的 anchor 及其觀測／可得時間。
- calibration version、decision policy version、digest；替換校正狀態不能修改既有模型或報告。

Prediction／Report provenance 固定為 model version + feature version + calibration version（純 NN 可無）+ decision policy version（純 NN 可無）+ input snapshot + as-of time。批次重跑可用同一組參照重建結果，過時校正與資料不足需回明確狀態。

NN 模型核准不自動涵蓋新的校正或決策版本。Energy／RUL 的評估與核准須綁定 model、calibration、decision policy 的版本及 digests；部署／批次執行核對同一組合，替換任一成員不得繼承不適用的舊評估。此組合治理是後續擴充，不能僅靠現有 neural manifest 的 Approval 宣稱完成。

## 4. 程式擴充映射

下表左欄為現存檔案；右欄為後續修改或擬新增位置，尚未實作。

| 目前位置 | 計畫變更 |
| --- | --- |
| [API schema](../src/api/ml_schemas.py)、[Recipe resolver](../src/infrastructure/datasets/ml_recipes.py) | 增加版本化 EBM intent 與受控 workload resolution；保留 MLP schema；不將任意 dictionary 視為合法模型設定 |
| [Dataset DTO](../src/domain/datasets/models.py)、[Snapshot reader](../src/infrastructure/datasets/snapshots.py)、[Catalog](../src/infrastructure/persistence/ml_datasets.py) | 保留既有二特徵型別；新增 `src/domain/datasets/ebm.py`、`src/infrastructure/datasets/ebm_snapshots.py`，以 schema version 選讀取器 |
| [Training runner](../bootstrap/ml_training_runner.py)、[ML wiring](../bootstrap/ml_wiring.py) | 依解析後的 recipe 選 dataset／trainer；新增 `src/plugins/features/fdc_spectrogram_v1.py`、`src/plugins/models/ebm_cnn_v1.py`、`src/plugins/training/ebm_trainer_v1.py` 與分組 sampler |
| [Evaluator](../src/infrastructure/serving/evaluation.py) | 新增 `src/plugins/evaluation/ebm_v1.py` 計算純指標，由評估 adapter 載入精確 Bundle／資料並套用版本化 policy；不能沿用 classifier accuracy 0.75 |
| [Supervisor finalization](../src/application/training/supervisor.py) | 從固定 workload descriptor 取得 evaluator／compatibility ID；包括評估失敗的 ERROR 紀錄，不能繼續硬編碼 `classifier-v1` 或錯誤的 runtime provenance |
| [Artifact verifier](../src/infrastructure/serving/artifacts.py)、[Runtime](../src/infrastructure/serving/pytorch_runtime.py) | 按已批准 contract 檢查必要檔案、schema、大小與 code digest；新增 `src/infrastructure/serving/ebm_runtime.py`，由 Bootstrap 注入 model factory |
| [Prediction DTO](../src/domain/prediction/models.py)、[Bounded executor](../src/infrastructure/serving/bounded_prediction.py)、[Serving bootstrap](../bootstrap/ml_serving.py) | 增加 EBM input/output 與回應驗證；保留 physical-computation capacity／timeout；取消「所有模型輸出必須 class_id 0/1」的共用假設 |
| [依賴宣告](../pyproject.toml)、[Role images](../Dockerfile.ml) | EBM feature/training 需要明確宣告 NumPy／Pandas／SciPy／Torch 的相容版本，報告角色另加 Matplotlib；移除計算路徑對 IPython display 的依賴，固定環境並維持 management 輕量啟動 |
| [Training ports](../src/application/training/ports.py)、[Worker](../bootstrap/ml_worker.py) | 保留現有 local 契約；Remote 路線才新增 `src/infrastructure/execution/databricks_jobs.py`，同步處理持久提交、對帳與 ACK 差異 |
| 批次分析尚無相應能力 | 新增 `src/application/analysis/`、`src/domain/analysis/`、分析 Plugin 與 Report Store adapter；各階段有持久識別、狀態與失敗結果 |

Domain／Application 不 import 具體 EBM Plugin。新的程式目錄只在其工作包實作時建立；先寫空的 Stub 不算完成能力。

Data Preparation 與 Batch Analysis 各自定義成功契約：前者須有完整發布的 DatasetSnapshot，後者須有固定輸入／模型／校正參照與完整發布的 Report Artifact。[TrainingRepository.finish](../src/application/training/ports.py) 需要 model binding 與 evaluation ID，不能原封不動用作資料或報告工作的終態條件；可以共用可靠性模式，但要保留各能力的業務結果。

## 5. 依序工作包

每個工作包的完成證據需列出 source revision、fixture／snapshot digest、runtime 與實際執行的測試。未執行項目記為待驗證；合成 fixture 僅證明工程契約，不能代替真實資料的 PoC 數值一致性。

| ID／順序 | 可交付範圍 | 依賴 | 退出條件 |
| --- | --- | --- | --- |
| EBM-WP-01 | Notebook source hash、設定／函式索引、Replay 語意與合法小型 fixture／reference outputs | Notebook；真實對照需模型方提供資料與執行結果 | 能明確識別 feature、NN、Energy、RUL 四種 reference；沒有 reference 的項目維持 pending |
| EBM-WP-02 | EBM dataset／recipe schema、immutable local snapshot importer；特徵／目標映射與時間欄位 | WP-01 的欄位／語意 | 拒絕 shape/order/group/digest 錯誤；同 ID/version 不可替換；MLP import/request 回歸通過 |
| EBM-WP-03 | 純函式 Spectrogram／Normalizer、CNN、Loss、sampler、CPU trainer 與角色依賴環境 | WP-02 | 固定資料 feature parity、finite loss/gradient、分組邊界、checkpoint/resume；合理數值容差寫入測試設定；乾淨環境可 import/run 且不依賴 IPython |
| EBM-WP-04 | Bundle export/load、純 NN runtime、EBM evaluator／policy 接點與 local Golden I/O | WP-03 | 無 Spark／訓練資料的乾淨程序可載入並重建輸出；驗證的是匯出的同一 Bundle；policy 缺失不可冒充品質通過 |
| EBM-WP-05 | 接入 Job／worker／registry／MLflow、固定 NN deployment 與 API | WP-04 | 重送、取消、resume、評估失敗／重試與部署回歸；真 MLflow round trip 有獨立證據；MLP 路徑維持可用 |
| EBM-WP-06 | Databricks ingestion，以及依選定策略的 export 或 remote executor | WP-02；實際存取環境 | 固定 Delta／全部 CSV 來源、併發隔離、發布失敗與重讀一致性；Remote 另通過 §6 的恢復契約 |
| EBM-WP-07 | CalibrationSnapshot、Batch Analysis Job、不可變表格／圖表／Report Artifact | WP-04～06 | 相同模型／校正／輸入可重播；缺少歷史或 CDF 時回明確不可用；回溯 RUL 清楚標示 offline |
| EBM-WP-08 | Temporal 軌道與線上 Energy／RUL 評估 | WP-07；可得時間及目標定義 | 只使用 as-of 可得資料；通過 rolling-origin、晚到標籤、校正切換與獨立時間區間驗證；品質門檻有實測及決策依據 |

WP-06 的來源介面／metadata 設計可與 WP-03～05 並行；第一個 CPU vertical slice 可使用已取得的本機不可變 fixture，不必先完成遠端 executor。每個工作包可再拆成 schema／純計算／整合 PR；跨層 dispatch 必須連同相應驗證一起交付。

## 6. 執行位置的決策與可靠性

| 策略 | 選用條件 | 需完成的契約 |
| --- | --- | --- |
| Snapshot Export → local/container PyTorch | 資料允許移出來源環境，匯出與搬移成本可接受 | 固定 Delta／CSV 來源、Parquet／NPZ schema、checksum、原子發布、受控讀取與完整資料量的記憶體／時間量測 |
| Databricks Remote Executor | 訓練需留在 Databricks，或搬移負擔不適合 export | 固定執行定義、持久 submission key／external run ID、排隊／執行／取消／未知狀態、結果 materialization、接管／對帳與資源 profile |

Notebook 的雙環境操作不能單獨決定採用哪條策略。Remote 也不會自動解決 `.toPandas()` 與整批 Tensor 的記憶體需求。

現有 [Supervisor](../src/application/training/supervisor.py) launch 後同步監看；[Worker](../bootstrap/ml_worker.py) 等 Job 終態及 transport snapshot 完成才 ACK。LLD 的 External Submitted 是後續擴充：只有 durable execution ref、spec digest、owner/revision 與可重新領取的 reconciliation work 同交易持久保存，才可 ACK 提交命令，Job 仍保持 RUNNING。

Remote 驗收至少包含：提交成功但回應遺失；run ID 儲存前程序崩潰；worker 重送；遠端 queued／running 時重新接管；取消請求與完成競爭；遠端不可查時保持未知而不重複啟動；完成後 artifact 下載失敗再對帳。短 timeout、50ms 本機 polling 與本機 path 假設必須改成遠端 profile；不能只替換 `launch()`。

## 7. 驗收矩陣

以下是待實作的測試案例，不是已執行結果。

| Case | 操作／反例 | 必須觀察到的結果 |
| --- | --- | --- |
| EBM-AC-01 | 同一原始 fixture 重建 feature；交換訊號或 Pad 順序 | 正確順序在記錄容差內相同；錯誤 metadata 被拒絕，不能靜默產生另一個合法輸入 |
| EBM-AC-02 | 增加測試期或未來 CDF／Lot | Temporal 訓練 targets、Normalizer 及訓練集合 digest 不變；Replay 若變動需顯示其版本語意 |
| EBM-AC-03 | 同 Lot 多 wafer、不同 recipe／segment、小於合法 group size | 映射與權重符合基準；sampler 不越 group；不合法 batch 明確失敗 |
| EBM-AC-04 | 不同 feedback、Aux Head=True、切換 Fold 設定 | Replay 的 feedback 不影響 eval 輸出；未實作 Aux／Fold 選項被拒絕；新增行為使用新版本 |
| EBM-AC-05 | 訓練中止後以相同 checkpoint 恢復 | 資料／設定 digest 匹配；恢復 epoch／RNG／sampler；重複命令不另起第二次有效訓練 |
| EBM-AC-06 | 刪除訓練資料與 Spark 存取，只給固定 Bundle | 純 NN 載入與 Golden I/O 成功；不重算訓練統計；篡改權重／Normalizer 導致拒絕 |
| EBM-AC-07 | CPU／其他裝置或 Torch 版本變更 | 未批准相容組合拒絕；批准組合以已記錄容差通過，而非要求未證實的 bitwise equality |
| EBM-AC-08 | EBM 評估結果或 evaluation policy 不存在／不匹配 | 不使用 classifier accuracy fallback；無匹配評估證據不得核准或部署 |
| EBM-AC-09 | 同 snapshot 併發發布／中途失敗；Pad CSV 改變 | 不覆寫可見版本；不完整版本不可讀；新內容有不同 digest，來源變更可追溯 |
| EBM-AC-10 | calibration 前 N 個 Lot 的 CDF 晚到 | as-of 模式不能使用未到資料；依版本策略回不可用或使用仍有效的舊校正，不偷看完整測試窗口 |
| EBM-AC-11 | RUL anchor 位於目前 Lot 之後；其他 cycle 晚於 as-of | 線上路徑排除；離線重播允許時輸出明確 retrospective 標記與來源 |
| EBM-AC-12 | 輸出 raw NN score、calibrated energy、RUL；僅替換 calibration／policy | schema、單位與 provenance 可區別；不將 latent std 宣稱為已校正 uncertainty；新組合不可沿用僅綁定舊模型／校正組合的評估核准 |
| EBM-AC-13 | EBM 與 MLP 同時提交／部署、逾時或 rollback | 契約互不混用；既有 MLP 回歸通過；運算未結束不提早歸還容量；rollback 保留固定版本識別 |
| EBM-AC-14 | 真 MLflow 註冊／下載，以及實際來源／執行環境 | digest、版本與報告綁定一致；跳過或未執行的環境測試不計為整合通過 |
| EBM-AC-15 | 評估拋錯、品質政策拒絕、資料／分析階段成功 | ERROR 證據記錄正確 evaluator／policy；品質拒絕不等於訓練執行失敗；資料／報告成功各有完整發布證據，不偽造 model binding |

驗證工具沿用既有 boundary checker、受影響模組的型別／lint 與 `tests/ml/`。新增案例依工作包放入 `tests/ml/ebm/`（提案）；只有相關程式落地後才執行對應測試。此文件變更本身以連結、路徑與內容一致性檢查為主。

## 8. 待確認輸入與可先進行的工作

| 決策／輸入 | 影響的工作 | 目前仍可進行 |
| --- | --- | --- |
| 小型合法 FDC／CDF／Pad fixture、原環境設定與 reference outputs | 真實 PoC parity（WP-01／03） | 契約、合成資料測試、純模型擷取；結果標為 engineering smoke |
| Feedback／QK／Aux／Fold 的預期語意 | 新版演算法；Replay 則可記錄已觀察行為 | 將不支援選項明確拒絕，建立忠實重播版本與模型單元測試 |
| Lot 級或 wafer 級預測目標 | Feature 與 output schema 的產品版本 | 保留 Lot→wafer 映射，避免誤稱已實現 wafer-specific 特徵 |
| 資料可搬移性、來源權限、完整資料量與可用 compute | WP-06 策略及資源 profile | 本機固定 snapshot 的 CPU 流程、remote handle／狀態契約草案 |
| CDF 可得時間、校正更新規則、RUL 預測時點與 horizon | WP-08 線上有效性 | 純 NN Serving、版本化離線批次分析與 as-of 欄位 |
| 預測品質、業務成本與延遲／容量門檻 | 正式 evaluation policy／線上驗收 | 計算可重現指標、蒐集測量；不自行沿用 demo 0.75 或虛構核准門檻 |

這些是對應工作包的輸入條件，不是暫停全部實作的總閘門。下一個可開始的程式工作為 WP-01～04 的 CPU slice：先取得／定義 fixture 與 Replay 契約，擷取純 Feature／CNN／Loss，再做到可獨立 save/load 的 NN Bundle；工程正確性與時間外推品質分開記錄。
