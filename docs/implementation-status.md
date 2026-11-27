# 實作與驗收狀態

更新：2026-11-19。基準為已合併 PR #11 的 master `55dd226`。
本頁集中記錄現行能力與剩餘工作；設計文件、測試結果與正式環境核准分開判定。

## 已交付與證據

- CPU MLP／EBM：不可變 Dataset、受控 Recipe／Workload Registry、子程序訓練、
  checkpoint、精確 Bundle 評估、MLflow／S3、明確 Approval、固定版本部署與預測。
- Data Ingress：File 與 Databricks SQL Warehouse batch adapter、核准來源與模板、
  OAuth M2M、固定 Delta version、有界 JSONL、持久發布與故障對帳。
  Raw Snapshot 不會自動變成訓練資料；EBM DatasetBuilder 是明確的 operator 步驟。
- 治理與協調：SQL 是 Job／Attempt／Outbox／Evaluation／Approval／Route 的權威；
  Redis 是 technical snapshot，RabbitMQ 是傳輸。取消、deadline、drain 與重送保留 fencing。
- 部署：既有 local／Compose／外部服務 VM profile，以及 opt-in K8s 原生 Serving、
  Pod-aware Training 與完整受治理生命週期驗收。

PR #11 的最終 source `43b660626dd01cb365a9c5864a509487bb9a619f` 已完成：

| 驗收 | 實際結果 |
| --- | --- |
| [K8s governed acceptance #7](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44233983953) | 新叢集從零執行 12 項主檢查，含 7 項 Training 故障情境；無 checkpoint continuation |
| [ML／Compose #96](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44233983865) | `FUNCTIONAL_BACKEND_ACCEPTED`；1169 passed、零 failure／error／skip、無 blockers |
| [Foundation #20](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44233983780) | profile、Secret／CA bootstrap 與邊界通過 |

同一實作的 [Git 留存證據](evidence/k8s-acceptance-2026-11-18/qualification.json)
固定 source `fe10413`、映像與原始 artifact 雜湊；其後的 README／文件／證據 commit
另由上述最終 CI 完整重跑。這些是合成資料、隔離服務與單節點 Minikube 的工程證據。
完整範圍與限制見 [Acceptance review](reviews/k8s-acceptance-2026-11-18.md)。

## 六個 K8s 工作包

| 工作包 | 狀態 | 規範／操作 |
| --- | --- | --- |
| Foundation | PR #7 已合併 | [Secret／CA 與初始化](deployment/kubernetes-staging-runbook.md) |
| Storage Contracts | PR #8 已合併 | [掛載、完整性與故障](deployment/kubernetes-storage-contracts.md) |
| Serving Runtime | PR #9 已合併 | [位址、ACTIVE 恢復與路由](deployment/kubernetes-serving-runtime.md) |
| Training Runtime | PR #10 已合併 | [Pod identity、ownership、停止與 ACK](deployment/kubernetes-training-runtime.md) |
| K8s Acceptance | PR #11 已合併 | [完整治理生命週期／故障、獨立 Compose 回歸](deployment/kubernetes-acceptance.md) |
| Production Operations | 尚未完成 | 公司叢集與 CSI、多節點／partition fencing、最小權限、TLS／MLflow、備份還原、容量／告警／SLO |

## 尚未完成或需獨立核准

- 真實 Databricks Warehouse／企業 MLflow 的身份、權限、TLS、資料與 artifact 資格。
  protected `databricks-live` 依核准 fixture 選用；略過不算外部驗收成功。
- 核准企業資料的 Notebook 數值 parity、代表性品質政策與人工模型品質核准。
- 自動 ingestion-to-build orchestration、Databricks Remote Executor、Kafka streaming。
- CalibrationSnapshot、Temporal／as-of 與 rolling-origin、calibrated Energy／RUL、
  Batch Analysis／Report Store；見 [EBM 後續計畫](ebm-integration-plan.md)。
- Production Operations 與正式 Go-Live；所有現有 `production_accepted` 旗標仍為 false。
  GPU、DDP、HPO 不在目前 CPU 工作負載範圍。

## 驗證入口

使用 Python 3.12／Torch 2.6 CPU 目標映像，從 repository root 執行。
品質工具的實際 scope 與依賴以 [ML workflow](../.github/workflows/ml-ci.yml) 為準：

```sh
python scripts/check_boundaries.py
python scripts/check_ingestion.py
python scripts/check_deployment.py
python scripts/check_serving_runtime.py
python scripts/check_training_runtime.py
python scripts/check_k8s_acceptance.py
python scripts/verify_ml_acceptance.py --output-dir var/acceptance/local
```

本機檢查不等同完整服務驗收。完整 Compose 命令與判讀見
[維運／回歸手冊](ml-runbook.md#完整-compose-回歸)，K8s 命令見
[Acceptance 手冊](deployment/kubernetes-acceptance.md)。不得對 production 資料執行合成驗收。

## Runtime source management

Databricks initialization supports connection-only setup. Authorized operators
register immutable, policy-governed source versions through the Management API.
Workers resolve exact SQL catalog definitions at execution. Legacy templates
and worker-only credentials retain their existing contracts.
See [runtime source management](runtime-source-management.md) for permissions,
lifecycle behavior and separate external qualification requirements.

## Governed metadata discovery

Opt-in Management discovery uses a dedicated Databricks identity and independent
ml:catalog:read grants. Ingestion clients can browse currently eligible registered
sources through available-sources. See [configuration, APIs and limits](runtime-source-management.md#governed-runtime-metadata-discovery).
External Databricks qualification and production approval remain separate.


## Prediction endpoint discovery

The feature implementation adds tenant-authorized endpoint list/detail on the existing Gateway.
It reuses active SQL routes and model bindings, with additive verified public contracts and
separate reviewed usage revisions. Legacy incomplete metadata remains explicit. Publication/
backfill is an authenticated operator CLI step. See the
[delivered API and operator workflow](plans/prediction-endpoint-discovery.md#wp2-delivered-contract-and-operator-walkthrough).
Target-environment production qualification remains separate.


## WP3 dataset discovery and lineage

Implemented on the WP3 feature branch synchronized with master: bounded ingestion job/snapshot discovery,
dataset family/version/detail and authorized lineage APIs; additive SQL receipt metadata and
atomic registration through the existing EBM publisher. Existing incomplete history remains
explicit; no automatic backfill or HTTP dataset build orchestration. Local SQLite/PostgreSQL
engineering verification is recorded in the [WP3 report](reviews/wp3-dataset-discovery-report.md).
External full-stack Compose/K8s and enterprise Databricks qualification are NOT EXECUTED for
this work package. `production_accepted=false`. WP4–WP7 remain outside this delivery.

## WP4 training recipe and job discovery

The WP4 feature branch serves approved recipe and resource metadata, scoped job pages,
the resolved spec and persisted fit metrics through the existing Management API.
See the [training discovery workflow](ml-runbook.md#training-recipe-and-job-discovery-wp4).
Submission and deployment approval continue using the existing contracts.
