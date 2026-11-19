# 文件與程式碼對照：2026-11-19

對照基準：已合併 PR #11 的 master `55dd226`，以及其後文件整理分支。
本次核對重要公開契約、操作入口、設定、部署宣告與既有資格證據；不修改 ML
核心／演算法／runtime manifests。原始測試報告維持原 bytes，歷史文稿從固定 Git
版本取回。這是文件一致性審查，不是新 runtime、真實資料或正式環境驗收。

## 維護入口與程式來源

| 重要範圍 | 對照來源 | 維護文件 |
| --- | --- | --- |
| Clean Architecture、DTO／Ports、角色 DI | [boundary checker](../../scripts/check_boundaries.py)、[ML wiring](../../bootstrap/ml_wiring.py)、[modules](../../bootstrap/components.py) | [架構](../architecture.md)、[模組規範](../module-spec.md)、[Backend 設計](../software-design.md) |
| ML Recipe／workload 靜態註冊與擴充 | [approved workloads](../../src/domain/training/workloads.py)、[runner](../../bootstrap/ml_training_runner.py) | [Workload Contract](../workload-extension-contract.md) |
| ML 環境鍵、預設、timeout 關係與 profile | [MLSettings](../../bootstrap/ml_config.py)、[IngestionSettings](../../bootstrap/ingestion_wiring.py) | [完整設定參考](../configuration-reference.md) |
| CLI、帳號／Identity 與權限 | [ML CLI](../../bootstrap/ml_cli.py)、[Actor aliases](../../src/domain/ml.py)、[Identity adapter](../../src/infrastructure/ml_identity.py) | [README](../../README.md)、[API 操作契約](../ml-runbook.md#api-與-client-契約)、[通用教學](../developer-guide.md) |
| HTTP DTO／body bounds／errors | [ML HTTP](../../src/api/ml_http.py)、[ML schemas](../../src/api/ml_schemas.py)、[EBM schemas](../../src/api/ebm_schemas.py)、[body limiter](../../src/api/ml_body_limit.py) | README、API 操作契約、[EBM](../ebm-implementation-status.md) |
| Training key TTL、retry／cancel、SQL state／Outbox | [Training service](../../src/application/training/service.py)、[SQL repository](../../src/infrastructure/persistence/ml_training.py)、[states](../../src/domain/training/models.py) | API 操作契約、[維運／recovery](../ml-runbook.md) |
| Worker／Control、Pod identity、stop／drain／ACK | [Worker](../../bootstrap/ml_worker.py)、[Training wiring](../../bootstrap/ml_training_wiring.py)、[reconciliation](../../src/application/training/reconciliation.py)、[ACK](../../src/application/training/delivery.py) | [Training Runtime](../deployment/kubernetes-training-runtime.md)、[ADR-020](../adr/020-pod-training-runtime.md) |
| Immutable Dataset、Builder receipt／lineage／bounds | [DatasetBuilder](../../bootstrap/dataset_builder_cli.py)、[builder port](../../src/application/datasets/builder.py)、[builder plugin](../../src/plugins/features/ebm_dataset_builder_v1.py) | EBM 使用說明、[Day-1 §9](../deployment/day1-deployment-runbook.md#9-核准-datasetversion-的-trainingserving-smoke) |
| File／Databricks、來源 grant／template／OAuth、publication／cleanup | [Ingestion wiring](../../bootstrap/ingestion_wiring.py)、[CLI](../../bootstrap/ingestion_cli.py)、[adapters](../../src/infrastructure/ingestion/adapters/)、[Raw store](../../src/infrastructure/ingestion/storage/) | [Data Ingress](../data-ingress-implementation.md)、[Databricks](../databricks-ingestion-runbook.md) |
| MLP／EBM compute、checkpoint、bundle／evaluation | [trainers](../../src/plugins/training/)、[bundle evaluator](../../src/infrastructure/serving/evaluation.py)、[EBM runtime](../../src/infrastructure/serving/ebm_runtime.py) | EBM 使用說明、[相容性](../ml-runtime-compatibility.md)、[EBM 後續未完成項](../ebm-integration-plan.md) |
| 人工 Approval、環境／digest、rollout／rollback／reservations | [Registry service](../../src/application/model_registry/service.py)、[Deployment controller](../../src/application/deployments/service.py)、[SQL Deployment](../../src/infrastructure/persistence/ml_deployments.py) | README、ML 維運、[Serving Runtime](../deployment/kubernetes-serving-runtime.md) |
| Serving 啟動、Gateway route identity、timeout／capacity | [Serving](../../bootstrap/ml_serving.py)、[Gateway](../../bootstrap/ml_gateway.py)、[bounded executor](../../src/infrastructure/serving/bounded_prediction.py) | 設定參考、ML 維運、Serving Runtime |
| Image targets、source／digest、secrets／CA／migration | [Dockerfile.ml](../../Dockerfile.ml)、[strict entrypoint](../../bootstrap/deployment_entrypoint.py)、[preflight CLI](../../bootstrap/deployment_cli.py)、[K8s init](../../deploy/k8s/base/files/prepare_runtime.py) | 相容性、[Day-1](../deployment/day1-deployment-runbook.md)、[Foundation](../deployment/kubernetes-staging-runbook.md) |
| Compose 與全部 K8s profiles、PVC／cache／RBAC／故障 | [Compose](../../compose.ml.yaml)、[external profile](../../compose.ml.external-mlflow.yaml)、[K8s 宣告](../../deploy/k8s/) | [部署索引](../../deploy/k8s/README.md)、[Storage Contracts](../deployment/kubernetes-storage-contracts.md)、五份 K8s runbooks |
| Readiness、metrics、trace 與備份還原邊界 | [Management](../../bootstrap/ml_api.py)、[ML metrics](../../src/infrastructure/ml_metrics.py)、[Ingestion metrics](../../src/infrastructure/ingestion/persistence/metrics.py)、[telemetry](../../bootstrap/ml_telemetry.py) | ML 維運、設定參考、Day-1 Go／No-Go |
| 工程驗收、執行證據與正式環境剩餘 gates | [workflows](../../.github/workflows/)、[K8s verifier](../../scripts/verify_k8s_acceptance.py)、[Backend verifier](../../scripts/verify_ml_acceptance.py) | [實作狀態](../implementation-status.md)、[K8s Acceptance](../deployment/kubernetes-acceptance.md)、[原始證據](../evidence/) |

## 本次補正

- `deploy/k8s/README.md` 原先只描述 Foundation；改列出五個已交付工作包及對應
  overlays／runbooks，說明每個 fresh namespace 的用途及第六包尚未完成。
- README 補上 K8s 部署選擇；新增設定參考，集中所有 MLSettings 環境鍵與
  Ingestion timing／limits，區分 Python defaults、profile overrides 與 strict entrypoint。
- ML 維運補上權限、Training key TTL／retry／cancel、實際 states／errors／body limits、
  aggregate metrics 與 health 能證明的範圍；不把設計 SLO 或告警當成已實作指標。
- Day-1 補齊 RawSnapshot→明確 operator DatasetBuilder→DatasetVersion 銜接；
  歷史測試數改指向統一狀態頁，避免誤認成當前 release 的驗收。
- Foundation 補上現行 profile 導流、歷史硬體觀測的限制與 Secret／CA 私有副本行為；
  HLD／LLD 明確指向實際操作與設定，保留設計／歷史來源的性質。
- 文件檢查擴及 `deploy/**/*.md`；manifest 仍只列 tracked source，排除本地原稿／資料。

## 判定與維護規則

上述重要實作與操作契約已有可從 README／docs 導讀抵達的維護入口。
設定值與 CLI flags 另以 source／`--help` 對照；連結、標題、SVG／JSON 語法及 manifest
以文件 CI 驗證。這不表示逐一複製每個內部函式或所有歷史文稿；新功能須更新表中
對應指南，歷史 evidence 與 target-design 不用來宣稱當前實作。

本次本地檢查：29 個 MLSettings 環境鍵與 14 個 Ingestion wiring 環境鍵均可在
設定參考找到；12 組實際 CLI `--help` 與文件使用的參數一致。文件連結／heading／
SVG／JSON、helper Ruff／format 與 dependency boundaries 通過。保留的 36 個
evidence files 及通用教學 22 個 Python blocks 與 `55dd226` 完全相同。這些檢查
只驗證文件／入口一致性；完整 runtime 資格仍依實作狀態頁所列 CI 與原始 receipts。

公司叢集／CSI／partition fencing、network-policy enforcement、真實 Warehouse／MLflow、
企業資料品質／數值 parity、Secrets rotation、SLO／告警、容量與實際 backup／restore
仍須獨立工程及現場證據；[Production Operations](../implementation-status.md#六個-k8s-工作包)
未完成，`production_accepted` 仍為 false。文件補齊不改變這個判定。
