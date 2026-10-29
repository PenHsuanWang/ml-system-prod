# Project Constitution — PyTorch ML System

> 狀態：Draft · 版本：0.1 · 日期：2026-10-28  
> 此為**供審查的建議條款**，尚未由 Repository 維護者核准或套用到程式碼。

## Principle I. Architecture boundaries are mandatory

- 系統沿用 `python-backend-server-template` 的 Domain、Application、API、Infrastructure、Plugins、Bootstrap 分層。
- Domain 不依賴 FastAPI、PyTorch、MLflow、資料庫、Taskiq 等 Framework／SDK。
- Application 定義它實際消費的窄 Port，不因新增產品能力而修改通用平台的 Domain／API。
- Bootstrap 是 composition root；API 和 Worker 不以全域 Service Locator 動態搜尋任意類別。
- 新的能力應保留通用 Template 預設功能中性，避免將 ML 相依硬寫進基本 FastAPI 啟動。

## Principle II. Separation of control and computation

- Management API 不在 HTTP Request 或事件迴圈內執行神經網路訓練。
- PyTorch Training 使用受控獨立 Process／Container；Serving 使用獨立 Runtime 與部署角色。
- 訓練和推論分開配置容量；所有高成本運算都必須受資源預算、timeout 和 shutdown policy 限制。

## Principle III. Durable business state and at-least-once handling

- TrainingJob、TrainingAttempt、Idempotency、Outbox、Approval、Deployment 由持久應用儲存負責。
- Message Broker 的重送是正常情境；Consumer 必須冪等、Claim 必須原子化，終態不可被過期 owner 覆寫。
- 不聲稱 RabbitMQ／Taskiq 提供 exactly-once execution；當外部副作用不確定時進入對帳流程。
- Redis 技術性快照、Lease 與快取不等於業務最終權威紀錄。

## Principle IV. Reproducible and governed ML

- 訓練必須固定 Dataset Version、Recipe Version、Model Architecture、Code/Image Digest 與 Resource Profile。
- MLflow 追蹤 Experiment、Run 與 Model Registry；不得代替應用的 Job State 或 Approval State。
- 訓練完成、評估通過、核准、部署 ACTIVE 是不同決策。
- Serving 僅載入被核准、相容且具有來源驗證的固定模型版本，不能每次請求動態選最新模型。

## Principle V. Safe extensibility

- 外部請求不得提供任意 Python import path、模型反序列化檔案路徑、Shell 命令、宿主機 GPU ID 或未核准的架構名稱。
- Extension 必須由已核准的 Plugin Registry 選擇，使用版本化 schema 和契約測試。
- 不可信模型 Artifact 不可直接進入 PyTorch 反序列化與 Serving。

## Principle VI. Security, tenancy and observability

- 新增的所有 ML API、背景命令與模型查詢都必須執行 Tenant／Owner／Permission 檢查；只在 HTTP 層檢查不足。
- Secret 不記錄於 Run Tag、日誌、Artifact、Trace 或 Feature Spec 範例。
- Job、Attempt、Run、ModelVersion、Deployment 必須有可追溯的關聯識別。
- 每一執行角色具有角色適用的 liveness／readiness；Serving readiness 在模型驗證及 warm-up 前不得為 True。

## Principle VII. Test before release

- 每個 Feature 必須具備可測試的 requirements、正常／例外／並行情境，及對應的驗收結果。
- PR 必須通過 format/lint、strict typing、unit、integration、合約、安全掃描與受影響的 E2E gate。
- 改動 Python/PyTorch／MLflow、作業系統、CPU/GPU Runtime 或模型格式時，必須重新進行相容性驗證。
- Production 可靠性與 SLO 只有在實測、簽核且能重現證據後才可宣稱合格。

## Governance

此 Constitution 是全域原則，不覆蓋 HLD／LLD 的角色邊界。當 Feature Spec 與已核准架構原則衝突，須記錄 ADR、影響範圍、相容性測試與核准，不能由 Coding Agent 自行靜默修改原則。實際採用 GitHub Spec Kit 時，應將審查後的內容合併到其生成的 `.specify/memory/constitution.md`。
