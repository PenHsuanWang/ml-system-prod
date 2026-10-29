# 工程交付狀態

交付日期：2026-10-26（Asia/Taipei）。交付目錄：`/Users/pwang/Developer/python-backend-server-template`。

**交付判定：可交付給開發團隊，作為通用 Python Backend 範本與產品開發基線。** 本次必要程式、CI、依賴鎖定、開發規範與驗證證據均已更新在此專案。架構文件為 PBP-SAD-001 v1.3，開發手冊為 DG-001 v1.1；Python distribution 仍為 0.1.0，文件版本不代表套件 release 已發佈。

## CI／pre-commit 增補交付（2026-10-26）

在 `0ac0bda3035d17e90cd01670a3045a4271ce871c` 之上加入 12 個共用 hooks、CI `quality` 與 `CI required` gate、過期工作取消及獨立 Bandit 報告。工具版本納入 dev hash lock，runtime／build locks 與正式 Python 邏輯不變。

本 clone 的 hook 已安裝；其他團隊仍須按 [操作規範](ci-precommit.md) 各自安裝。Linux Python 3.11 與 macOS Python 3.13 的單元／coverage 與品質驗證通過；另完成 macOS Python 3.13 的套件及開發手冊驗證，詳見 [增補驗證](validation.md) 與 [證據](evidence/ci-precommit-2026-10-26/README.md)。

此次增補仍為未提交的本機變更；原先已存在但未追蹤的 `.gitleaks.toml` 保留原樣，須與新的 pre-commit 設定、文件及證據一併納入後續提交。尚未替使用者 commit／push、設定遠端 required checks 或重新核准產品部署。

## 已完成的交付範圍

- 保留九個分層 package、明確 bootstrap、API／worker lifespan、OOP／Ports & Adapters／Application Service 與建構式注入；正式業務能力與演算法註冊清單仍空白。
- 新增 disabled task backend，純 API 可多程序 production 且不依賴 RabbitMQ／Redis 背景工作。memory 保留單程序開發；distributed 保留共享工作。停用時明確拒絕 tasks／job 查詢，保留受保護同步 kernel。
- API／worker 共用安全 JSON logging，一般工作帶 schema_version 與 request_id，兼容舊 envelope；無效輸入於入列／保存狀態前拒絕，執行 context 正確還原。
- 固定建置工具、runtime／dev／build hash locks、唯一更新流程；具 wheel／sdist 真實安裝、coverage、guide、依賴／靜態／secret／容器及 workflow 檢查。
- 固定基底 image digest；安裝檢查後移除 runtime 不需要的 pip／ensurepip，避免保留其內附易受影響套件。容器 smoke 同時檢查非 root 與無安裝工具。
- 更新架構圖、設計規格、module 功能規範、開發手冊、工程基線、CONTRIBUTING、ADR 與原建議採納紀錄。

實測項目、數字與限制以 [validation.md](validation.md) 為唯一彙整；原始結果存於 [先前交付證據](evidence/handover-2026-10-26/README.md) 及 [CI 增補證據](evidence/ci-precommit-2026-10-26/README.md)。既有歷史證據未改寫。

## 團隊拿到專案後

1. 依 [工程基線](engineering-baseline.md) 建立鎖定環境，執行品質與 package／guide 檢查。
2. 依 [開發手冊](developer-guide.md) 在 domain/application/api 與對應 adapter／plugin／config 增加同名 capability，由 bootstrap 明確選入。
3. 依 [協作規範](../CONTRIBUTING.md) 交付自己的 schema、授權、測試、資料 ownership、非同步副作用與操作文件。

已用新副本原樣提取中性 items 範例，完成同步／背景測試及真 MongoDB／RabbitMQ／Redis 容器驗證，含同租戶他人與跨租戶拒絕；該範例沒有加入本專案正式註冊清單。

## 發佈與部署界線

這是可交付的工程範本；特定產品的正式上線仍需其部署與營運驗收。尚未實作或核准的項目明列如下：

- GitHub Actions 工作已定義並完成本機等效檢查／actionlint；沒有宣稱遠端 CI 已跑綠，也未設定組織 required checks／reviewer。
- 尚未建立 registry／staging／production 的 CD，未部署正式服務。metrics／tracing exporter、worker heartbeat、壓測、HA／備份恢復與 SLO 由目標平台需求決定。
- Image 通過「有可用修正的 HIGH／CRITICAL 必須為零」門檻；完整掃描仍列出 Debian 基底未提供修正版的項目，包括 44 筆 HIGH。這不是零漏洞聲明，產品上線前須由維運／安全 owner 評估及追蹤更新。
- SDK 目前仍完整安裝，尚未拆成 minimal extras；沒有通用業務自動 retry、持久 exactly-once、outbox 或資料 downgrade。
- 原生 Windows 未驗證；Windows 使用者採 WSL2／Linux container。已保存 Linux Python 3.11 及 macOS Python 3.13 的驗證。
- 授權 metadata 由權利人決定；本次未自行套用開源授權。

先前交付驗證當時，修改已落地本機工作樹；保留既有 Git 歷史，尚未新增 commit、push 或建立遠端 release。來源雜湊與交付範圍保存在證據目錄，可供後續版本提交及簽核。
