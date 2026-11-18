# ML Runtime 與相容性邊界

目前目標為 Linux CPU／Python 3.12／Torch 2.6.0 CPU。實際支援判斷使用固定
source／image／artifact、對應測試及明確 Approval，不使用舊的 sklearn 提案表。

| 範圍 | 來源／事實 |
| --- | --- |
| Production role image | [Dockerfile.ml](../Dockerfile.ml) 的 `production` target；Torch 2.6.0 CPU、MLflow skinny 3.6.0，須提供完整 source SHA |
| Dev／acceptance tooling | 同檔案的 `acceptance`／`k8s-acceptance` targets；額外工具依各 target 安裝，不推定與 production 的所有套件版本一致 |
| MLflow server | [Dockerfile.mlflow](../Dockerfile.mlflow) 的 3.6.0／SQLAlchemy／PostgreSQL driver 組合；外部服務另行驗證 |
| Approved workloads | [Workload Contract v1](workload-extension-contract.md)；未知 Recipe／handler／schema fail closed |
| 真實受驗 source／manifest | [K8s／Compose qualification](evidence/k8s-acceptance-2026-11-18/qualification.json)、[完整報告](reviews/k8s-acceptance-2026-11-18.md) |

Manifest digest 不是 Docker config image ID。Model Bundle／資料／Normalizer／Golden
I/O 與 Evaluation／Approval 必須指向同一組不可變內容。更換 code、framework、
serializer、OS／arch 或任一 image，需依對應 profile 重新 build/load/predict、錯誤拒絕、
停止／cleanup、rollout／rollback 驗證；不可只比較版本 major number。

現有工程證據涵蓋 MLP／EBM、實際 MLflow／S3、local／Compose 與單節點 K8s。
它不核准任意 sklearn／PyFunc／ONNX 模型、GPU、跨節點 CSI、企業 MLflow 權限或
正式部署。Production 的 dependency locks／SBOM、安全、資源／SLO 與還原證據
仍是 [待完成工作](implementation-status.md#尚未完成或需獨立核准)。
