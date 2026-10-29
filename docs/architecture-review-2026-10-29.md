# PyTorch ML System 設計文件審查

審查日期：2026-10-29（Asia/Taipei）。審查基準：ML-HLD-001 v1.1、ML-LLD-001 v1.1、ML-SPEC-001 v0.2，以及 Constitution、SDD README、既有 Review Report、Manifest 與圖檔。

本次為獨立的設計審查，沒有修改原始設計、勾選核准條件，或將建議視為已批准的架構決策。以下描述的是文件矛盾與契約缺口，並非已觀察到的程式缺陷。

## 判斷

整體架構方向成立，可以繼續收斂 M0 與首個 Feature 的技術 Plan。Control / Training / Serving 分開執行、App DB 保存業務真相、Outbox 配合原子 Claim、固定版本 Serving，以及將 Evaluation / Approval / Deployment 分開，都是適合此需求的基礎。

但目前仍有 **5 項 P1、2 項 P2** 應處理。尤其 Job 狀態機雖然跨文件一致，Attempt 與 Deployment 的狀態機卻不能完整承接文件自己要求的取消與故障恢復流程。建議先修訂相關契約，再實作對應 capability，避免 Domain、資料表與故障處理各自採用不同語意。

此處 P1 指可能破壞工作可靠性、模型載入或部署真實性的設計問題；P2 指需要在相關功能實作前補齊的資料契約或文件治理問題。沒有將「尚未實作」本身列為缺陷。

| ID | 等級 | 發現 | 建議完成時點 |
| --- | --- | --- | --- |
| DR-01 | P1 | Attempt 缺少未啟動取消路徑，且 LOST 終態與恢復要求矛盾 | M0／Domain 契約定稿前 |
| DR-02 | P1 | Deployment 將切流不確定導向 FAILED 終態，無法表達後續對帳成功 | M5 Controller 實作前 |
| DR-03 | P1 | Outbox 的 PUBLISHED 條件只寫 publisher confirm，缺少可路由與持久化契約 | M1 Relay 實作前 |
| DR-04 | P1 | 完整訓練結束才 ACK，未對齊 Broker acknowledgement timeout | M1 Receiver／M2 長任務整合前 |
| DR-05 | P1 | Serving Artifact 契約缺少可重建模型的配置與已擬合前處理內容 | M2 Export／M3 Artifact 契約定稿前 |
| DR-06 | P2 | Evaluation／Approval 資料契約未明確綁定被評估的 artifact 與報告 | M3／M5 治理實作前 |
| DR-07 | P2 | 根目錄與 docs/ 並存不同版本的 HLD／LLD | 下一次文件更新前 |

## DR-01 — Attempt 狀態機無法承接取消及執行恢復

**位置**：[LLD §2.4](pytorch-ml-system-lld.md) L239–255；§6.4 L785–798；§15.1 L1367–1369。

Attempt 只有 `RUNNING → CANCELLED`，沒有 `PENDING → CANCELLED`；但取消流程明確要求尚未啟動時關閉 Pending Attempt，且不啟動運算。`STARTING` 階段確認停止後如何結束 Attempt 也未定義。若照狀態機建立 Domain guard，Job 可以取消，Attempt 卻無法合法結束；若繞過 guard，又會形成兩套契約。

另一個直接矛盾是 `STARTING/RUNNING → LOST → [*]`。目前 LOST 的觸發條件是 ownership 不確定或 heartbeat 過期，但 Reconciler 又允許發現程序仍存活時恢復 ownership／heartbeat。這要求同一 Attempt 能繼續執行，與 LOST 為終態的圖不符。

**建議**：補上從 PENDING、STARTING 取消的事件、先決條件及結果。將「執行狀態」與「owner 是否失聯」分開，或引入非終態的 UNKNOWN／RECONCILING；若保留 LOST 終態，則必須限於已確認無法恢復的執行，不能單靠 heartbeat 過期判定。恢復 ownership 時仍須驗 execution identity 與 owner generation，不能直接接受舊 owner 更新。

**應增加的驗收情境**：取消 Pending Attempt 不啟動子程序；取消與 Claim 競爭只產生一種合法結果；Supervisor 失聯但 child 存活時，對帳可恢復同一 execution，且不新增第二份 fit。

## DR-02 — Deployment 的不確定狀態被提前終結

**位置**：[LLD §2.4](pytorch-ml-system-lld.md) L258–277；§10.3 L1083–1103；§15 L1354。

Deployment 圖定義 `ACTIVATING → FAILED: reconcile / rollback needed`，並將 FAILED 畫為終態；但文字及 Rollout 流程要求切流結果未知時先查 Router，若已切至新 generation，則補寫 ACTIVE。

**觸發情境**：Router 已成功切換 N+1，但回應遺失或 App DB 寫入失敗。若 Controller 依圖在等待失敗後提交 FAILED，之後對帳確認 N+1 正在承接流量，卻沒有合法狀態轉換能表達 ACTIVE。資料庫的失敗判定將與實際流量矛盾。

**建議**：未知切流結果時保留 ACTIVATING 並記錄 reconciliation 狀態，或增加明確的非終態。只有確認 activation 未生效或補償動作完成後，才可進 FAILED；對帳時依 Router 觀測值與 generation CAS 收斂，不直接覆寫終態。

**應增加的驗收情境**：分別注入 Router response lost、Router 成功後 DB commit 失敗、Controller 在切流後被 kill，驗證實際路由與 DB 最終一致，且舊 Controller 不能覆寫較新的 generation。

## DR-03 — Publisher confirm 不足以作為 PUBLISHED 的完整條件

**位置**：[LLD §3.4](pytorch-ml-system-lld.md) L463；§6.1 L709–716。

目前規範寫收到 Broker confirmation 後標記 Outbox published，未定義 exchange／queue／binding、message persistence 與 unroutable return 的處理。

RabbitMQ 對無法路由到任何 queue 的訊息也可以回 publisher confirm；若使用 mandatory，會先發送 `basic.return`。因此不能只以 confirm ACK 推定命令已進入可消費的持久佇列。[官方 Publisher Confirms 說明](https://www.rabbitmq.com/docs/confirms#when-will-published-messages-be-confirmed-by-the-broker)

**觸發情境**：部署時 binding 遺漏，Relay 發布獲得 confirm 後標 PUBLISHED，實際沒有 Consumer 會收到命令。若沒有另訂補償機制，Job 只會停留等待，甚至最後因 queue deadline 失敗。

**建議**：鎖定 Broker topology 與 queue 類型；定義持久化設定、mandatory 或等效可路由保證，將 returned／nack／unknown 分別映射到 Relay 的結果。只有符合既定路由與持久化契約才標 PUBLISHED，其餘沿用相同 command_id 有界重試並告警。

**應增加的驗收情境**：移除 binding、指定不存在的 exchange、Broker 在 confirm 前後重啟；不能發生「Outbox 已標成功但命令沒有可恢復交付證據」。

## DR-04 — 長任務 ACK 策略未涵蓋 Broker 的確認逾時

**位置**：[LLD §6.3](pytorch-ml-system-lld.md) L761–769；§6.4 L783；§12.1 L1167–1171。

Supervised Local 規範要求 Job 終態或安全 retry handoff 持久化後才 ACK，因此整段訓練、artifact 上傳、evaluation／registration、故障對帳都可能處於 unacked 狀態。設定表與 Deadline 清單沒有包含 Broker delivery acknowledgement timeout，也未定義它與工作期限的關係。

RabbitMQ 官方文件列出預設確認逾時為 30 分鐘；適用時，逾時會關閉 channel 並重新入列。支援範圍與版本／queue 類型有關，例如 4.3 起限 quorum queues，必須依最終鎖定版本驗證。[官方 Consumer Timeout 說明](https://www.rabbitmq.com/docs/consumers#acknowledgement-timeout)

**觸發情境**：一個合法的長訓練超過該期限，程序仍運作，但 Broker 先重送。Claim 可以防止再次 fit，卻不能自行避免 channel 反覆關閉、確認失效與恢復阻塞。

**建議**：對目前 Local 方案，定義有界的最長 unacked 期間，將 Broker timeout、Taskiq 任務／shutdown 設定與完整工作期限一起資格驗證。若改採「持久接受執行後提早 ACK」，需要另行設計 durable handoff 與恢復責任，不能只提早呼叫 ACK。不要將 HTTP timeout、heartbeat 或 DB lease 視為 ACK timeout 的替代品。

**應增加的驗收情境**：在測試環境使用縮短且受 Broker 支援的 timeout，驗證期限附近的訓練完成、channel close、重連及重送，不產生第二份運算，且命令最終能安全確認。

## DR-05 — Artifact 尚不足以重建完整的推論流程

**位置**：[LLD §2.2](pytorch-ml-system-lld.md) L168；§7.2 L836–838；§8.3 L944–960；§10.2 L1063–1066。

TrainingSpec 允許 model config，例如 `hidden_units: [64, 32]`；Artifact Manifest 則只有 architecture key／version，沒有 constructor config 或其不可變參照。前處理雖在文字中要求保存，manifest 例子只列 weights 與 feature schema，沒有將已擬合前處理的內容／digest、feature order、輸出 label mapping 定為必備或依 Recipe 明確可選的契約。

PyTorch 載入 state_dict 時仍須先建立對應模型實例；可信任 architecture code 本身不能取代該次模型的配置。[官方 state_dict 載入說明](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html#save-load-state-dict-recommended)

**觸發情境**：同一 `mlp_v1` 使用不同 hidden units 訓練，乾淨的 Serving Image 只拿到現有 Manifest／DeploymentSpec，無從確定建立哪個模型；即使權重能載入，若改用預設 scaler、feature order 或 label mapping，仍可能產生語意錯誤的預測。

**建議**：定義一個可獨立還原的 Inference Bundle：模型架構與配置、weights、input/output schema、訓練所得 preprocessing state、feature ordering、label mapping，以及各內容的 digest。無需前處理或 label mapping 的 Recipe 要明確標示不適用。DeploymentSpec 綁定整個 Bundle，不只權重。

**應增加的驗收情境**：在沒有訓練 staging 目錄、沒有隱含 Recipe 預設值的乾淨 Serving Image 中，分別載入兩種 model config；對 raw input 執行完整 preprocessing → predict → output mapping，與離線參考結果在明確 tolerance 內一致。

## DR-06 — 評估與核准需要更精確的不可變證據關聯

**位置**：[LLD §3.2](pytorch-ml-system-lld.md) L362–379、L407–409；§9.1 L977–987。

Evaluator 文字要求消費固定 artifact，並允許不重新 fit 而重新評估同一版本。但資料表最小契約將 EvaluationResult 關聯到 attempt 與 policy，沒有明定被評估的 artifact／model binding digest；Approval 則沒有明定引用哪筆 EvaluationResult 或 EvaluationReport digest。

單靠 Attempt 不足以表達不同 checkpoint／export 結果，也不足以區分同一模型先後執行的評估。若審查頁讀到報告 A，而核准時選到另一份報告 B，僅保存 model + policy version 不能完整還原核准依據。這是資料契約缺口；文件尚無實作可證明已經錯誤使用報告。

**建議**：建立不可變 EvaluationSpec 或等效欄位，固定 artifact digest、benchmark digest、Evaluator version、QualityPolicy version／digest；EvaluationResult 與 Approval 明確串接 evaluation_id／report digest。訓練首次評估與之後重新評估應各自保存證據，不能覆蓋歷史報告。

**應增加的驗收情境**：同一 Attempt 的不同 artifact、同一模型的不同 policy、評估與核准並行時，批准紀錄都能明確指出被審查的模型內容與完整評估證據。

## DR-07 — 存在兩組未明確封存的設計基準

**位置**：[根目錄 HLD](../pytorch-ml-system-hld.md)、[根目錄 LLD](../pytorch-ml-system-lld.md) 與 [docs/ HLD](pytorch-ml-system-hld.md)、[docs/ LLD](pytorch-ml-system-lld.md)。

README 已指向 docs/ 的 v1.1，這點正確；但根目錄仍有 v1.0 的同名文件，且未標示已被取代。根目錄 HLD 有 589 行、LLD 有 1423 行；docs/ 分別有 670、1524 行。差異包含里程碑與已修正的可靠性契約，並非只有格式或 SVG。

**影響**：人員或 Coding Agent 以檔名搜尋時可能讀到舊基準，重新帶入既有 Review 已修正的問題。

**建議**：以 docs/ 作為唯一可用基準；舊版移至明確封存目錄，或在檔首標示 superseded 並連至新版。README 加入文件權威順序及變更流程。不要維護兩組同名、同樣看似有效的設計。

## 已存在的未決事項

以下在原文件與既有 Review Report 已明確列為待決，本次不重複計算為新增發現；但進入相應實作階段前仍須處理：

- Request canonicalization 的確切算法、schema/default 版本與 golden vectors；24 小時 idempotency retention 的核准、過期鍵重用與並行回收協定。
- LocalProcessExecutor 的 stable execution identity、launch 成功但 DB ref 尚未保存的恢復方法；不能只用可能被重用的 PID 判斷程序身份。
- 首個 Dataset／Recipe／CPU MLP／Evaluation fixture，以及代表性輸入大小與品質基準。
- JSON Schema／OpenAPI、Job query／attempt history／cancel response 的完整欄位與錯誤對照。
- Taskiq Receiver 的實際 ACK／error 路徑、取消與完成的競爭優先序、deadline 與 retry budget。
- MLflow Artifact Proxy 或 Direct 模式、租戶隔離的實際 enforcement、服務身份及存取範圍。
- Template 的確切 source commit、依賴與 image 版本；既有文件中的 CI 失敗記錄不能直接當成本資料夾或當前上游的現況。
- 工作與 artifact 留存、跨 App DB／MLflow DB／Object Store 的備份還原及 RPO／RTO；SLO 的數值、窗口與負責人。

首個 MVP 建議維持現有 CPU MLP 單一路徑，不把 CNN／LSTM、GPU、Kubernetes 或自動重訓加入首輪。現有相依服務與執行角色已多，先以真 PostgreSQL／Broker 配合測試替身驗證工作可靠性，再接入真訓練與 MLflow，符合目前的增量交付方向。

## 本次檢查證據與限制

| 檢查 | 結果 |
| --- | --- |
| FILE-MANIFEST.json | 原清單 24 筆檔案存在，bytes 與 SHA-256 全數一致 |
| Markdown 本地連結／圖片 | 所有被掃描的本地檔案目標均存在；未驗證外部網址及標題 anchor 的完整有效性 |
| SVG XML | 15 張皆可解析；不代表視覺排版或內容已渲染驗證 |
| Job 狀態機 | LLD 與 Spec 均有 24 條非起訖狀態轉換，端點集合相同；其中 20 條帶事件標籤，與既有 Review 的標示可相容 |
| Attempt 圖路徑 | 無不經 STARTING／RUNNING 的 PENDING → CANCELLED 路徑；LOST 無返回 RUNNING 的路徑 |
| Deployment 圖路徑 | FAILED 無前往 ACTIVE 的路徑；需先解決未知切流與終態語意 |
| 執行期／CI | 沒有執行 Python 系統測試、真 Broker／DB／MLflow／PyTorch E2E 或 Mermaid 渲染；本資料夾尚無相關實作 |

原本的 [REVIEW-REPORT.md](../REVIEW-REPORT.md) 已處理多項跨文件修訂；本報告補充狀態機內部可達性與端到端契約問題，不取代原先的 Owner 核准與實測要求。此新增報告不屬於原始 FILE-MANIFEST.json 的封裝清單。
