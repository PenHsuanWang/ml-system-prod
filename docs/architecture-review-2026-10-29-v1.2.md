# ML-LLD-001 v1.2 更新複查

審查日期：2026-10-29（Asia/Taipei）。對象：[pytorch-ml-system-lld.md](pytorch-ml-system-lld.md) v1.2 Draft，共 1612 行。

審查時 LLD SHA-256：`982b19eb157664c07c5085afa4d5ea3c4c3fc76ca1e2944c5c9ef042a2998ed0`。本報告補充 [前次審查](architecture-review-2026-10-29.md)，不修改 LLD 或代替 Owner 核准。

## 結果

前次 7 項問題中，**4 項已在文件契約層面修正、3 項部分修正**；另外發現評估／匯出／註冊順序與新增證據契約之間的相依矛盾。本次整理為 **2 項 P1、2 項 P2**。修訂已明顯改善，但仍不建議直接將相關章節當作已收斂的實作契約。

| 原問題 | v1.2 判定 | 核對結果 |
| --- | --- | --- |
| DR-01 Attempt 取消／LOST | 文件層面已修正 | L248、250 補上未啟動／啟動中取消；LOST 為非終態，L257–260 有可驗證的恢復／終結出口；L266–268 補充 identity／fencing／交易條件 |
| DR-02 Deployment 不確定性 | 部分修正 | 增加 RECONCILING、移除 ACTIVATING 直接 FAILED；但仍缺少阻止未知切流請求晚到執行的協定，見 RV3-01 |
| DR-03 Outbox 發布證據 | 文件層面已修正 | L482–489 定義 durable topology、persistent、mandatory、return／nack／confirm；不再把 kiq() 回傳視為充分證據 |
| DR-04 長任務 ACK timeout | 文件層面已修正 | L797–805 區分有界 Local 與 durable external handoff，列出版本／queue type／timeout／重送驗收要求 |
| DR-05 Inference Bundle | 文件層面已修正 | L978–1029 補上 constructor config、已擬合前處理、輸出映射與 golden I/O；但產生／評估／註冊的順序另有 RV3-02 問題 |
| DR-06 評估／核准關聯 | 部分修正 | L1057 文字契約已補強，§3.2 資料模型仍未同步，見 RV3-03 |
| DR-07 文件基準 | 部分修正 | L15 宣告 docs/ 為候選權威基準；根目錄舊版、README／Manifest 尚未完成同步，見 RV3-04 |

以上「已修正」僅表示文件已提出足以處理原問題的契約與驗收要求，不表示測試已執行或功能已完成。

## RV3-01 — P1：觀察到舊路由，不代表未知的切流請求已失效

**位置**：LLD §2.4 L284–292；§10.3 L1155、L1171–1174。

新增 RECONCILING 的方向正確。但 Rollout 圖仍將「觀察到 N 是 active」直接導向「新版本 FAILED／保留 N」。Router 的既定條件只有 `expected_generation=N`，沒有明確要求終止或隔離先前已送出、結果未知的 activation operation。

依目前契約可以構造以下交錯順序：

1. Controller 送出 `activate(expected=N, target=N+1)`，請求仍在網路或 Router 中等待，呼叫端逾時。
2. Reconciler 透過另一次查詢看到目前仍是 N。
3. 依圖標記 N+1 FAILED，並依失敗清理流程撤除其 Runtime。
4. 先前的 activation 請求晚到；因為實際路由仍是 N，原 CAS 依然成立，Router 切到已清除的 N+1。

這是根據文件條件推導出的競爭反例，不是已觀察到的 Router 實作故障。App DB 的 revision／owner CAS 無法單獨阻止已離開該交易的 Router 操作。

**建議修訂**：將「舊路由仍生效」與「原切流操作已確定結束或被 fencing」分成兩個條件。只有確認原 activation 不會再生效，且候選 Runtime 不再被路由引用，才進 FAILED／清理。可選擇持久 operation_id 與可查詢／可終結的操作狀態、Router 強制驗證的 fencing epoch，或以更高 generation 固定指回舊模型，使舊 CAS 無法通過；若沒有這種證據，維持 RECONCILING。補償仍須遵守 rollback 建立新 generation 的原則。

**建議驗收**：暫停原 activation 請求 → 查詢舊 route → 嘗試結案／清理 → 放行舊請求；驗證舊請求被 Router 拒絕或候選未被提前清理，不能將流量切至已退休 Runtime。

## RV3-02 — P1：固定評估證據與訓練流程的產生順序矛盾

**位置**：LLD §7.1 L861–863；§7.2 L873–874；§8.2 L968–972；§8.3 L984；§9.1 L1045、L1057。交叉參照：HLD §7.1 L455–458。

目前同時存在三個要求：

- 訓練 lifecycle 先執行 held-out Evaluation，再 Export Artifact／Manifest，最後才 Registry binding。
- Evaluator 要消費固定 Artifact；v1.2 更要求每份 EvaluationResult 不可變地綁定 ModelVersionRef 與 Artifact／Manifest digest。
- 上傳於 registration 之前的 Manifest 本身已包含 `registry.version`。

因此，評估開始時，契約要求的最終 Bundle／Manifest digest 與 ModelVersionRef 尚未依既定流程產生；上傳 Manifest 時也尚未從 Registry 取得該版本。若後補 Manifest 的 Registry version，整份 Manifest digest 又會改變，必須另外定義其封存與綁定協定，不能暗中修改原有不可變證據。

MLflow 的 `create_model_version` 以 source artifact URI 建立版本，並回傳含 version 的 ModelVersion；不能以查詢「目前最大版本 + 1」預先猜測，因為存在並行註冊。[官方 API](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html#mlflow.client.MlflowClient.create_model_version)

**建議修訂**：採用與現有 HLD lineage 相同的主順序：

`fit → export/freeze Inference Bundle → register candidate version → evaluate exact registered bundle → persist EvaluationResult → manual approval → deployment`

將 registration 結果放在 App DB 的 ModelVersionBinding 或獨立 binding metadata；預先封存的內容 Manifest 不必內含尚未分配的 Registry version。註冊候選版本並不賦予部署資格，評估／核准 Gate 繼續生效。若仍需保留註冊前的評估，應明確區分為 provisional 結果，並定義其與最終 Bundle 的一致性驗證；不能將它直接當作目前 §9.1 所要求的最終不可變 EvaluationResult。

**建議驗收**：首次註冊、兩個候選版本並行註冊、registration response lost／對帳時，最終被核准及 Serving 載入的 manifest digest 必須等於 Evaluator 實際使用的 digest，且封存後不因寫入 Registry metadata 改變。

## RV3-03 — P2：新增不可變關聯尚未同步至最小資料模型

**位置**：LLD §3.2 L379–397、L424–426；§9.1 L1057；§10.1 L1092–1116。

§9.1 現在要求 Approval 固定引用 evaluation_id、report digest 與相同 model binding，但 ER 圖與標示為「必要欄位／限制」的 Schema 表仍保留舊版：EvaluationResult 主要關聯 Attempt，Approval 只有 Model Binding／Environment／Policy，沒有上述關聯。這不是要求在設計階段補齊全部 SQL，而是需要讓最小資料契約與新的 MUST 一致。

**建議修訂**：至少補上 EvaluationResult → ModelVersionBinding、Artifact digest／Report digest，以及 Approval → EvaluationResult 的關係與不可變約束。Deployment 的受控持久紀錄或 DeploymentSpec 應固定引用所採用的 approval_id（或等效不可變證據），避免之後重新查詢時才選擇一筆「目前可用的核准」。同一模型的重新評估必須新增紀錄，不能覆蓋既有核准所引用的結果。

**建議驗收**：模型 A 的 Approval 引用模型 B 的 Evaluation 必須被拒絕；同一模型存在兩份報告時，部署仍能指回當時核准的確切報告，而不受新增報告影響。

## RV3-04 — P2：文件權威宣告與封裝追溯尚未完全同步

**位置**：LLD L14–15；根目錄 HLD／LLD 檔首；README L7；FILE-MANIFEST.json L95–98。

LLD 已宣告 docs/ 為候選基準，改善了原問題；但直接開啟根目錄 v1.0 文件仍看不到 superseded 提示，README 仍寫 LLD v1.1。Manifest 仍保存原 v1.1 的 89602 bytes／SHA-256；目前 LLD 為 101203 bytes，故該筆驗證不再匹配，其他 23 筆仍一致。

另，LLD L14 寫「原始審查檔尚未取得」屬修訂當時的背景；目前同目錄實際已有該審查檔。應明確保留為歷史紀錄，或改為指向實際審查文件，避免閱讀者誤判追溯資料缺失。

**建議修訂**：舊版檔首標示 superseded 並連至新版，或移到 archive；README 更新版本。若 Manifest 要描述目前封裝，則重新產生；若刻意保留作 v1.1 封裝證據，則明確標示其版本與適用範圍，不將它用作目前工作目錄的驗證清單。本次僅報告差異，沒有改寫原封裝紀錄。

## 靜態核對

| 項目 | 結果 |
| --- | --- |
| Job 狀態機與 Spec | 均為 24 條非起訖轉換，端點集合相同 |
| Attempt 狀態機 | 14 條非起訖轉換；PENDING／STARTING 可取消，LOST 可恢復且無終態箭頭 |
| Deployment 狀態機 | 15 條非起訖轉換；存在 ACTIVATING → RECONCILING → ACTIVE；沒有 ACTIVATING → FAILED |
| 新增驗收 | ML-RV2-01～10 均已列出；其內容仍是待實作的驗收契約 |
| JSON 示例 | 7 個 JSON block 均可解析；未據此宣稱符合未定稿的 JSON Schema |
| LLD 本地連結／圖片目標 | 皆存在；未做標題 anchor 完整驗證 |
| SVG | 15 張皆可通過 XML 解析，未執行視覺渲染驗證 |
| FILE-MANIFEST | 1 筆 LLD 與舊封裝不同，其他 23 筆匹配 |

本次亦核對了新增 Broker 描述與 [RabbitMQ Publisher Confirms](https://www.rabbitmq.com/docs/confirms#when-will-published-messages-be-confirmed-by-the-broker)、[Consumer ACK Timeout](https://www.rabbitmq.com/docs/consumers#acknowledgement-timeout) 的對應語意；未發現原 DR-03／04 所指出的契約缺口仍然存在。

未執行 Python 系統測試、真 DB／Broker／MLflow／PyTorch E2E 或 Mermaid 渲染。前次已明確列為待決的 canonicalization、retention、版本鎖定及實測 SLO，本次沒有再重複列為新增問題。
