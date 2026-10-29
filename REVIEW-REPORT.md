# PyTorch ML System Architecture — Cross-Document Review Report

> Review ID：ML-REVIEW-2026-10-29  
> 審查日：2026-10-29（Asia/Taipei）  
> 文件：ML-HLD-001 v1.1、ML-LLD-001 v1.1、ML-SPEC-001 v0.2、SDD README v0.2  
> 審查性質：文件契約、交互參照、狀態語意及交付前一致性檢查；非程式執行或 Production 驗收。

## 審查結論

外部 Review 提出的問題多數成立，已將可直接定稿的文件一致性問題修復。另有二項建議不可直接照文字取代：

1. **M1 Stub 對 `SUCCEEDED` 的語意**：不能讓正式 `TrainingJob.SUCCEEDED` 在不同里程碑表示不同意義。改以 test-only Fake Evaluator/Registry 滿足不變量，或僅標記 `TrainingAttempt` 運算完成、Job 不進入正式成功終態。
2. **Request Fingerprint**：必須區分用於 API 冪等的 `request_fingerprint`（經正規化的使用者公開意圖）與用於可重現執行的 `training_spec_digest`（完成後端版本解析的不可變執行規格）。如只比對後者，後端 Recipe／image 更新可使相同重送誤判為 409。

## 已完成修訂

| ID | 問題 | 審查判定 | 修正結果 |
|---|---|---|---|
| R-01 | HLD M0–M5、LLD M0–M7 | 成立，P0 | 統一以 LLD M0–M7 為權威；HLD 流程圖和 Gate 表全面改成八階段 |
| R-02 | HLD／LLD Open Decision ID 撞號 | 成立，P1 | `HLD-OD-01`～`HLD-OD-09`、`LLD-OD-01`～`LLD-OD-11`；跨文件決策仍建議彙總到 ADR Index |
| R-03 | HLD LLD Traceability 章節錯位 | 成立，P0 | 六個 Traceability Row 均依 LLD 實際目錄修正 |
| R-04 | LLD State Diagram 缺 6 條轉換 | 成立，P0 | 補齊六條並與 Spec 全部 20 條具語意標籤的轉換對齊；補充 post-accept failure/retry claim/cancel 不變量 |
| R-05 | `TrainingJob.SUCCEEDED` 與 M1 Stub 不相容 | 成立，但不宜直接放寬成功定義，P0 | 保留正式成功條件；M1 使用 test-only Fake Output/Evaluation/Registry Contract 或只測 Attempt 執行完成 |
| R-06 | Schema error 422／409 不一致 | 成立，P1 | 未支援的 Schema Version、Feature Schema／Signature mismatch → `422 SCHEMA_MISMATCH`；`409` 只供冪等／修訂衝突 |
| R-07 | SLO 標示 M3 | 成立，P1 | M4 起量測、M6 Pilot Hardening Exit 前核准 |
| R-08 | Spec 架構來源不含 Job Reliability 實作 | 成立，P1 | HLD §5–10，LLD §2–6、§15，移除 Out-of-Scope 的 LLD §7 |
| R-09 | Spec 技術中立 Invariant 指名 PostgreSQL | 成立，P1 | Spec 將資料庫廠牌改為持久權威儲存；PostgreSQL 細節保留在 LLD |
| R-10 | SDD README 參照路徑與預期 Repository 不符 | 成立（以 Repo 目標目錄為準），P1 | 使用 `specs/README.md` → `../docs/...`；獨立舊 ZIP 的相對路徑不代表部署後路徑 |
| R-11 | SDD README Acceptance Scenario IDs 不存在／引用錯誤 | 成立，P1 | FR-003 → AS-002A/C/D；FR-004 → AS-002B；FR-009 → AS-005A/C；FR-011 → AS-004D/005B |
| R-12 | Object-specific Auth 拒絕狀態未定稿 | 成立，P0 | 未認證 401；已認證但看不到特定 Job 的 Query/Attempts/Cancel 統一 404；可見但無取消動作權限 403；Submit 權限不足 403 |
| R-13 | Idempotency-Key Retention 未定 | 成立，但產品決策尚未批准，P0 | 文件新增 **24h / 86400 seconds 作為提案**、從首次接受起算、重送不延長、過期可能新建 Job；保留 Owner sign-off Gate |
| R-14 | Request Fingerprint Canonicalization 未定 | 成立，P0 | 將 request fingerprint／full spec digest 分離，定義 schema-versioned normalization 與 `latest` 禁用；具體 canonical JSON byte-level 演算法及 golden vectors 仍須納入 Plan／Contract Tests |
| R-15 | HLD 的 FR-02 僅描述 tenant+key，但 Spec 有 actor/operation | 額外發現，P1 | 將 HLD FR-02 scope 改為 tenant + actor + operation + key |
| R-16 | SVG 圖集中包含選取式 Job State 圖 | 文件補強 | SDD Spec 插入已驗證 SVG 並標註：SVG 為選取式圖示，完整狀態以 Mermaid／文字契約為準 |

## 仍未核准／不可跳過

- **ML-SPEC-001 保持 Draft**。`checklists/requirements.md` 中 10 項原有審核條件未自動勾選。文字修訂不構成 Owner 的簽核或已完成測試。
- **24 小時 Retention 為提案**。需確認 API 客戶端的重試期間、業務可能持續時間、存取稽核／保留政策、過期後重新提交的風險與租戶配額。
- **完整 Canonicalization 算法仍須鎖定**：欄位 allowlist／預設值版本、數值標準化（1 vs 1.0）、Unicode、空值、拒絕 NaN/Infinity、request schema 版本、golden test vectors；不得只寫 `json.dumps(sort_keys=True)` 就宣稱跨語言一致。
- **M1 假物件不可流入 Production Registry 或 Artifact Store**，Fake Evaluator／Registry 必須由測試用 Dependency Injection 明確替換。
- **具體 ACK persistence gate** 需要真 Taskiq Receiver／Database 整合驗證，不能由圖表推導出 exactly-once execution。
- **生產負載 SLO**、Queue Deadline、Retry Limit、Cancellation Grace、Artifacts/RPO/RTO，均需有負責人、數值與環境證據。
- **所用模板的 CI 狀況** 需於實作時重新以當前 GitHub Actions 與 Runtime Image 驗證，不以先前通過或失敗紀錄推斷新版本狀態。

## 提交前檢查結果

- 兩份 Architecture 文件的里程碑集合：M0～M7，一致。
- HLD／LLD 未決事項 ID：唯一且帶文件前綴。
- LLD vs. SDD TrainingJob 狀態機：20 條具標籤的轉換，對應端點完全一致。
- SDD README 中 AS IDs：均能在 ML-SPEC-001 找到。
- Markdown 的本地連結與圖片相對路徑：有效。
- SVG：15 張已通過 XML 解析。
- **未在此輪執行** Mermaid 實際渲染、Python CI、真 PostgreSQL／RabbitMQ、MLflow 或 PyTorch E2E；文字和靜態檢查結果不能取代前述驗證。

## 套用至 Repo 的建議位置

```text
repo/
  docs/
    pytorch-ml-system-hld.md
    pytorch-ml-system-lld.md
    diagrams/ml-system-c4/*.svg
  specs/
    README.md
    constitution.md
    templates/feature-spec-template.md
    001-durable-training-jobs/spec.md
    001-durable-training-jobs/checklists/requirements.md
```

目前仍為核對後的**建議修訂副本**，未替使用者提交任何 GitHub Commit 或 PR。
