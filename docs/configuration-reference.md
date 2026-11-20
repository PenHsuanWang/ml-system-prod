# 現行 Runtime 設定參考

以 [MLSettings](../bootstrap/ml_config.py)、[IngestionSettings](../bootstrap/ingestion_wiring.py)
及實際部署宣告為準。表中是 Python 預設值；Compose／K8s 可明確覆寫，production
必須提供核准值。`.env` 不會被所有 Python entrypoint 自動載入；Compose 使用
`--env-file`，直接執行 CLI 則須先配置該程序的環境。

首次安裝可用 [Interactive Setup](deployment/interactive-setup.md) 的 `ml-system init`。
它產生既有 profile／schema，並明確處理覆寫、備份與 private file permissions。

## Profile 與設定層

| 場景 | 設定來源與執行邊界 |
| --- | --- |
| 本機 demo | `MLSettings` 的 dev 預設；SQLite／local tracker；不是 release 資格 |
| 參考 Compose | [compose.ml.yaml](../compose.ml.yaml)；明確設定 dev、內建 DB／MLflow／broker；[產生器](../scripts/create_ml_env.py) 只產生此 profile 的私有環境檔 |
| 外部服務 VM | 獨立 [production Compose](../compose.ml.external-mlflow.yaml) 與 [.env 範例](../.env.ml.production.example)；[Day-1 手冊](deployment/day1-deployment-runbook.md) 定義 public host paths／ports／資源及 private role JSON |
| K8s | [profile 索引](../deploy/k8s/README.md)、[共用 ConfigMap](../deploy/k8s/base/configmap.yaml) 與各 overlay；helper 渲染私有 Secret／CA／source／image pins；Pod identity 由 Downward API 注入 |

`production` image 的 [entrypoint](../bootstrap/deployment_entrypoint.py) 要求
`ML_RUNTIME_ROLE`、絕對路徑 `ML_RUNTIME_CONFIG_FILE`，並在 exec 前載入每個 role
允許的私有欄位。Role 可為 migrate、management、relay、supervisor、control、gateway、
ingestion-worker、operator、serving；ingestion-worker／operator 的 ML 組裝角色為 management。
它不等同下表的 `ML_DEPLOYMENT_ROLE`。Private JSON 必須是該 UID 擁有的 regular file、
mode `0400` 或 `0600`，最多 64 KiB，路徑不經 symlink、拒絕重複及未允許 keys。
不要用環境變數預先注入同一組受保護連線／認證欄位來繞過 loader。

Production 強制 `prod`、MLflow、`postgresql+psycopg://`（Serving 無 App DB）、完整
40-hex source SHA 與 `sha256:<64 hex>` image manifest digest。MLflow URI 必須 HTTPS，
`ML_TRACKING_URI` 與 `MLFLOW_TRACKING_URI` 完全一致，提供
`MLFLOW_TRACKING_SERVER_CERT_PATH` 的有效 CA 並保持 TLS 驗證。Writer／reader
分別使用完整 Basic pair 或 token，且身份必須不同；角色欄位矩陣見 Day-1 手冊。
K8s Serving 的 private reader 由其專用 role 載入。

`docker compose exec`／`kubectl exec` 不繼承 PID 1 啟動後才載入的私有環境。
需要 secrets 的維護命令必須經過 role entrypoint；VM 使用手冊的 `compose run`，
K8s 使用受限 operator／既有驗收 helper，不能裸跑 CLI 當作等價操作。

## MLSettings 全部環境鍵

| 環境鍵 | Python 預設 | 用途／限制 |
| --- | --- | --- |
| `ML_DEPLOYMENT_ROLE` | `management` | management／relay／supervisor／runner／control／serving／gateway |
| `ML_DATABASE_URL` | `sqlite:///./var/ml.db` | App DB；非 dev 除 Serving 外須 PostgreSQL；production private file |
| `ML_MODEL_ENVIRONMENT` | `dev` | dev／staging／prod；Approval 必須匹配部署環境 |
| `ML_TRACKER_BACKEND` | `local` | local／mlflow；非 dev 必須 mlflow |
| `ML_TRACKING_URI` | `http://localhost:5000` | MLflow endpoint；不會自行替換 Compose 內建服務 |
| `ML_ARTIFACT_ROOT` | `var/artifacts` | Local tracker／materialization 路徑；K8s cache 與 durable artifacts 的用途見 Storage 手冊 |
| `ML_DATASET_ROOT` | `var/datasets` | 不可變註冊資料集，須持久保留 |
| `ML_EXECUTION_ROOT` | `var/executions` | execution／checkpoint／receipt／guardian／local Serving 證據；不可因 lease 過期刪除 |
| `ML_RABBITMQ_URL` | `amqp://guest:guest@localhost:5672/` | 開發值；production private file |
| `ML_QUEUE_NAME` | `ml.training.v1` | Worker 與 Relay 必須使用同一拓撲 |
| `ML_TRAINING_TIMEOUT_SECONDS` | `300` | compute timeout；正整數 |
| `ML_QUEUE_TIMEOUT_SECONDS` | `600` | queue deadline；正整數 |
| `ML_CANCEL_GRACE_SECONDS` | `5` | 停止寬限；正整數，不取代 physical stop proof |
| `ML_HEARTBEAT_SECONDS` | `2` | heartbeat 間隔；正整數 |
| `ML_STALE_AFTER_SECONDS` | `15` | stale threshold；嚴格大於兩個 heartbeat 間隔 |
| `ML_IDEMPOTENCY_RETENTION_SECONDS` | `86400` | Training key 有效期及 Worker Redis ACK snapshot TTL；不是 artifacts／Job 留存期 |
| `ML_MAX_PENDING_JOBS_PER_TENANT` | `100` | 每租戶非終態 Training admission 上限；正整數 |
| `ML_ACK_TIMEOUT_MS` | `1800000` | 嚴格大於 `1000 × (training_timeout + cancel_grace + 120)` |
| `ML_IMAGE_DIGEST` | `unqualified-development` | 非 dev 須真實 sha256 manifest digest；不是 Docker config image ID |
| `ML_CODE_REVISION` | `development` | 非 dev MLSettings 接受 40-hex SHA 或 sha256 digest；production entrypoint／K8s Serving 僅接受 40-hex SHA |
| `ML_EBM_QUALITY_POLICY_JSON` | 空字串 | 核准的 EBM policy；空值拒絕品質核准，不會阻止成功 fit；格式見 [EBM 使用說明](ebm-implementation-status.md#品質政策與固定-serving) |
| `ML_SERVING_BACKEND` | `local` | local／kubernetes；切換時依 Serving 手冊重新核准部署 |
| `ML_SERVING_NAMESPACE` | 空字串 | kubernetes backend 必須明確指定合規 namespace |
| `ML_SERVING_IMAGE_REPOSITORY` | 空字串 | kubernetes backend 必須指定 repository，runtime digest 來自凍結 DeploymentSpec |
| `ML_TRAINING_BACKEND` | `local` | local／pod；Pod profile 中只有 Worker dispatch、只有 Control stale takeover |
| `ML_TRAINING_NAMESPACE` | 空字串 | pod backend 的 supervisor／control 必填 |
| `ML_TRAINING_POD_NAME` | 空字串 | 同上，由 Downward API 提供 |
| `ML_TRAINING_POD_UID` | 空字串 | 同上，固定 Pod incarnation，不以 Pod name 取代 |
| `ML_TRAINING_CONTAINER` | 空字串 | 同上，supervisor／control |

Queue 與 compute／finalization deadline、broker ACK timeout、Taskiq drain 與 Pod
`terminationGracePeriodSeconds` 各自生效。Admission 凍結總 deadline 為建立時間加
queue 與 training budget；調大 broker ACK 不會延長 fit。更動 budgets 時必須同時
檢查 broker／Taskiq／guardian／Pod 的界線；不可把單一設定當成全域 timeout。

## 其他 ML／Serving／Identity 設定

| 鍵／常數 | 現行值／來源 |
| --- | --- |
| `ML_REDIS_URL` | Worker Python 預設 `redis://localhost:6379/0`；production private file；僅 technical ACK snapshot |
| `APP_MONGO_URI`、`APP_JWT_SECRET` | Gateway／Serving 必須提供；production private file；JWT secret 至少 32 bytes；授權仍查 current sessions／users |
| `APP_MONGO_DATABASE`、`APP_JWT_ISSUER`、`APP_JWT_AUDIENCE` | Gateway／Serving 預設 `backend`、`backend`、`backend-api`；須與 Identity 相符 |
| `ML_DEPLOYMENT_SPEC_PATH`、`ML_DEPLOYMENT_SPEC_DIGEST` | Controller 提供，Serving 啟動必需；不是 client 可指定的模型位置 |
| `ML_SERVING_EXECUTION_DIRECTORY`、`ML_SERVING_POD_UID` | 分別由 local executor／K8s Downward API 提供實體身份 |
| `ML_WAIT_FOR_SCHEMA` | K8s init 在值為 `true` 時最長等待 240 秒、每 2 秒查 schema；migrate／Serving 不使用此等待來建立資料表 |
| `MLFLOW_TRACKING_USERNAME`／`MLFLOW_TRACKING_PASSWORD`／`MLFLOW_TRACKING_TOKEN` | private writer Basic pair 或 token；Serving entrypoint 將其獨立 reader 映射到 MLflow client 使用的欄位 |
| `ML_SERVING_MLFLOW_USERNAME`／`ML_SERVING_MLFLOW_PASSWORD`／`ML_SERVING_MLFLOW_TOKEN` | private reader Basic pair 或 token；role 最小權限見 Day-1 矩陣 |
| `ML_PREDICTION_TIMEOUT_SECONDS` | [Serving](../bootstrap/ml_serving.py) 預設 `5` 秒；bounded executor 固定 capacity 2；逾時不代表底層計算已停止 |
| Gateway HTTP timeout／connection pool | [Gateway](../bootstrap/ml_gateway.py) 固定 10 秒／32 connections；不是前一列的設定 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 未設定則不建立 OTLP exporter；[telemetry](../bootstrap/ml_telemetry.py) 明確啟動，需 observability extra |

通用 Backend 的 `APP_*`、Identity、Taskiq 及 HTTP 設定另見
[Developer Guide](developer-guide.md) 與 [bootstrap/config.py](../bootstrap/config.py)。
實際 [Settings](../src/config/settings.py) 使用 `APP_` prefix 並讀取 `.env`；ML
Management 要求 `APP_IDENTITY_ENABLED=true` 及 Mongo／JWT。ML profiles 設定
`APP_TASK_BACKEND=disabled`，不以通用 Backend 的 memory jobs 代替 durable Training。
VM host 路徑／資源／ports 等 Compose interpolation 鍵見 production `.env` 範例；
image targets／版本見 [Runtime 相容性](ml-runtime-compatibility.md)。這些不是額外的
`MLSettings` 欄位；請核對渲染後 profile，不只核對 host shell。

## IngestionSettings 與 Raw Snapshot

| 環境鍵 | Python 預設 | 驗證／用途 |
| --- | --- | --- |
| `ML_INGESTION_ENABLED` | `false` | Management 是否組裝 Ingestion；接受 true／false／1／0 |
| `ML_DATABRICKS_ENABLED` | `false` | 啟用 Databricks adapter；同上；啟用時必須提供兩個核准設定檔 |
| `ML_DATABASE_URL` | `sqlite:///./var/ml.db` | 與 App DB 相同；schema 由明確 migration 建立 |
| `ML_RAW_SNAPSHOT_ROOT` | `var/raw-snapshots` | Worker 與 DatasetBuilder 共用相同持久 root |
| `ML_INGESTION_FILE_ROOTS_JSON` | `{}` | server-side approved roots；最多 128 entries／64 KiB，拒絕重複 keys |
| `ML_INGESTION_LEASE_SECONDS` | `30` | 秒，`0 < value <= 3600` |
| `ML_INGESTION_RETRY_DELAY` | `1` | 秒，`0 <= value <= 300` |
| `ML_INGESTION_MAX_ATTEMPTS` | `3` | 整數 1–100 |
| `ML_INGESTION_RECONCILE_LIMIT` | `10` | 整數 1–1000 |
| `ML_INGESTION_QUEUE_TIMEOUT` | `600` | 秒，`0 < value <= 86400` |
| `ML_INGESTION_MAX_PENDING` | `100` | 整數 1–100000 |
| `ML_INGESTION_POLL_SECONDS` | `1` | 秒，`0 < value <= 300` |
| `ML_DATABRICKS_CONNECTIONS_FILE` | 未設定 | 絕對路徑 approved connection mapping；開啟 adapter 必填 |
| `ML_DATABRICKS_SOURCES_FILE` | 未設定 | 絕對路徑 approved templates mapping；開啟 adapter 必填 |

Duration 必須有限。Connection／template 不接受 HTTP caller 自選；OAuth secret file
或 allowlisted access-token env、fixed Delta version、SQL／型別與 bytes／records 預算
詳見 [Databricks 手冊](databricks-ingestion-runbook.md)。SourceDefinition 的 row／byte／
timeout 上限與上述 Worker 設定不同；請求只能縮小核准預算。Ingestion durable
idempotency 不套用 Training 的 86400 秒 TTL。

Builder 必須使用相同 tenant／App DB／Raw root／Dataset root 與受信任 operator UID，
以 snapshot ID／manifest digest 解析已發布資料，輸出 immutable DatasetVersion 與 receipt。
它不輪詢來源、不啟動訓練、不核准模型；[完整輸入與 CLI](ebm-implementation-status.md#rawsnapshot-datasetbuilder)。
