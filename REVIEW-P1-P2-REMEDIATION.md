# 第二輪架構審查 P1/P2 收斂與修訂報告

> 日期：2026-10-29（Asia/Taipei）  
> 產物：`docs/pytorch-ml-system-lld.md` v1.2（**Draft，尚待正式核准**）  
> 基線：HLD v1.1、LLD v1.1、ML-SPEC-001 v0.2（原件未覆寫）  
> 限制：使用者提及的 `docs/architecture-review-2026-10-29.md` 未出現在目前可用的工作目錄或 GitHub `main`；本報告依使用者貼上的 5 P1／2 P2 摘要及可用的 LLD v1.1 核對。

## 逐項決議

| Issue | Risk | v1.2 Draft Resolution | 驗收案例 |
|---|---|---|---|
| P1-01 Attempt State | PENDING 無法取消；LOST 終態但仍可恢復 | PENDING→CANCELLED，LOST 改為非終態、以 verified execution+fencing/CAS 恢復；確認停止再終止 | ML-RV2-01～03 |
| P1-02 Deployment Activation | Router 回應遺失，錯誤 FAILED 但新流量已生效 | ACTIVATING→RECONCILING；觀察實際 Route 才轉 ACTIVE/FAILED；修正 rollback flow | ML-RV2-04 |
| P1-03 Outbox Publish | Unroutable confirm 誤報完成／非持久化 | mandatory+return detection+publisher confirm+durable queue+persistent delivery；不確定則同 command ID 重試 | ML-RV2-05～06 |
| P1-04 Training ACK | 長訓練超過 consumer_timeout，重送造成重 fit | M1 bounded/unacked policy + broker/version/queue contract；長時訓練改 durable external execution handoff ACK，需 App DB reconciliation | ML-RV2-07～08 |
| P1-05 Artifact Rebuild | Serving image 無法恢復 preprocessing、輸出映射 | Artifact Manifest v2 提案：architecture constructor、fitted transforms、input/output map、golden IO、Trusted digests | ML-RV2-09 |
| P2-01 Eval Approval | 已核准 report／模型不匹配 | EvaluationResult 綁 artifact/dataset/report/policy digest，Approval 綁 same evaluation/model refs；改版需重新核准 | ML-RV2-10 |
| P2-02 Document Authority | root v1.0 與 docs v1.1 混用 | `docs/` 為候選權威位置；原 v1.0 標為歷史基線；不得合併不同版本資訊 | Docs review |

## 對 M1 / SDD 的 Gate 影響

- `ML-SPEC-001` 的 Job 狀態機既有不變條件不放寬，Attempt 子狀態補強不取代 Job 狀態。
- M1 進入 `plan.md` 的必要設計簽核包括 P1-01、P1-03、P1-04（選定 broker version、queue type、Taskiq ack adapter、時間預算與 owner）。
- P1-02、P1-05 與 P2-01 在部署／Serving 工作包啟動前須核准，盡早在 HLD/LLD 保留接口與測試。
- M1 僅做 CPU MLP 短且有界的本機工作；**不可**把無限訓練時長對應到等候 terminal ACK 的設計。
- HLD v1.1 和 SDD v0.2 原件沒有在這次套件裡升版；進入正式 Approved 前，需再次檢查 `Job SUCCEEDED`、ACK Gate、Artifact Manifest v2 的交叉引用與 SDD scenario。

## RabbitMQ 官方來源

- https://www.rabbitmq.com/docs/confirms#when-will-published-messages-be-confirmed-by-the-broker
- https://www.rabbitmq.com/docs/publishers
- https://www.rabbitmq.com/docs/consumers#delivery-acknowledgement-timeout

## 非完成項目

本次是規格修訂與靜態驗證，沒有執行 PyTorch、Taskiq、RabbitMQ 或 Deployment Controller 的實際整合測試；不表示原 GitHub Repository 已更新或交付可執行實作。
