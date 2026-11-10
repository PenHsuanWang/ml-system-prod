# Day-1 部署與 Go／No-Go 操作手冊

本流程使用既有的外部 MLflow Tracking／Registry、Databricks SQL Warehouse，以及
已準備好的 PostgreSQL、MongoDB、RabbitMQ、Redis。部署拓撲是單一 Linux host／VM，
Controller 與各 Serving generation 共用受控 execution filesystem，Gateway 共用
Controller 的 network namespace。多主機、Kubernetes、NFS 與自動 Raw-to-Feature
轉換不在這份 profile 的驗收範圍。

`compose.ml.external-mlflow.yaml` 是獨立的 production candidate profile，**不與
`compose.ml.yaml` 合併**。它不建立內建 MLflow、MinIO 或基礎設施服務。根目錄
README 的 `create_ml_env.py` 與 `up --build` 則是參考開發環境的操作。

本文件提供可重複執行的程序，不代表外部環境已通過驗收。2026-11-09 的
`5ec1c6f` 已通過參考 Backend CI 與本地完整 864-case Backend 驗收；真實 Databricks、
這次 release 的外部 MLflow、production 容量與回復能力仍須取得自己的執行證據。
新部署 profile 的實作、隔離驗證與仍待完成的現場資格見
[2026-11-10 部署交付紀錄](../reviews/day1-deployment-2026-11-10.md)；參考環境的既有
CI 結果不能替代新 profile 或本次 production release 的驗收。

## 1. 接收版本化 release 與核准前提

部署工程師先取得下列交付物與負責人，不能用開發環境的預設值代替：

| 交付物 | 核對內容 |
| --- | --- |
| Release manifest／核准紀錄 | 環境、租戶、變更單、部署／覆核／值班負責人、回復決策人 |
| Immutable release image | Registry 中的 `repository@sha256:...`、目標 platform、SBOM／安全與來源建置證據 |
| Code revision | 核准的完整 40-hex Git commit；與 image 的 `org.opencontainers.image.revision` label 及來源證據一致 |
| 共用 runtime image | 所有 ML roles 使用同一個已核准 image；目前 `ML_IMAGE_DIGEST` 同時用於 Training／Serving 契約，不能任意混用不同 role digest |
| 外部依賴 | PostgreSQL、Mongo、RabbitMQ、Redis 與 MLflow 的 DNS／路由、TLS／信任、帳號、權限及維運窗口 |
| Databricks fixture | Workspace／Warehouse reference、允許 SELECT 的固定 Delta version、template、獨立核准的筆數與 SHA256 |
| 模型 smoke | 已核准且已註冊的 DatasetVersion、recipe／品質政策、預期輸入／輸出及人工核准者 |
| Storage 與資源 | Runtime UID／GID、受控 Linux filesystem、CPU／memory／PID／concurrency 上限及容量核准 |
| 回復集合 | App DB、Identity、MLflow metadata、artifacts、datasets、執行／路由證據與舊 release 的協調備份及 restore 計畫 |

Release 建置團隊以乾淨、已核准的 checkout 建置 `Dockerfile.ml` 的 `production`
target，提供 `ML_RELEASE_CODE_REVISION`，發布到核准 Registry 後交付實際 RepoDigest。
Day-1 operator 使用已發布的 digest，不在 production host 執行 `up --build`。
Image label 是比對資料，不能取代可信建置、簽章與來源核准。

此 profile 不提供基礎設施 provisioning、TLS ingress、憑證核發或 production 帳號
建立政策。Management／Gateway 預設僅綁定 host loopback；對外服務須使用已核准的
TLS ingress／network policy，再驗證實際使用者會經過的路徑。

## 2. 準備設定、Secrets 與 filesystem

從 `.env.ml.production.example` 複製出受保護、未追蹤的站台設定檔，填入核准值。
該檔保存 image、public endpoints、路徑與 limits；連線憑證放在 secrets 管理流程
交付的 private role JSON，不貼到聊天、PR、command arguments 或公開 logs。

主要設定：

| 設定 | 要求 |
| --- | --- |
| `ML_RELEASE_IMAGE` | 完整 `repository@sha256:<64 hex>`；所有服務一致 |
| `ML_IMAGE_DIGEST` | 與該 image reference 的 RepoDigest suffix 一致；不是任意抄入 Docker `.Id` |
| `ML_CODE_REVISION` | 與核准 release 及 image label 一致 |
| `ML_EXTERNAL_MLFLOW_URI` | 實際外部 HTTPS Tracking／Registry／Artifact Proxy endpoint |
| `ML_MLFLOW_CA_FILE` | Host 上核准的 PEM trust bundle，容器內唯讀 `/run/config/mlflow-ca.pem` |
| `ML_UID`／`ML_GID` | 可讀 private files、可寫 runtime storage 的核准非 root UID／GID |
| `ML_RUNTIME_ROOT` | 持久、受控的共用 runtime root，容器內 `/app/var` |
| `ML_DEPLOYMENT_CONFIG_DIR` | 各 role private JSON 的 host directory，位於 runtime root 外 |
| `ML_OPERATOR_DEFINITIONS_DIR` | 核准 SourceDefinition 的 host directory，operator 唯讀掛載為 `/run/ml/operator-definitions` |
| `ML_DATABRICKS_CONNECTIONS_CONFIG`／`ML_DATABRICKS_SOURCES_CONFIG` | 核准的 regular JSON files；容器內 `/app/config/databricks-connections.json`、`databricks-sources.json` |
| `ML_DATABRICKS_CLIENT_SECRET_FILE` | Worker 專用 OAuth secret；容器內 `/run/secrets/databricks_client_secret` |
| `ML_*_CPUS`／`ML_*_MEMORY`、`ML_RUNTIME_PIDS_LIMIT` | 每個 role 經核准的資源上限；範例不是容量／SLO 驗收 |
| `ML_API_CONCURRENCY`／`ML_GATEWAY_CONCURRENCY` | 經負載驗證的 API／Gateway concurrency |

每個 role 的 private JSON 以該 role 名稱命名：`migrate.json`、`management.json`、
`relay.json`、`supervisor.json`、`control.json`、`gateway.json`、`ingestion-worker.json`、
`operator.json`。它們各自唯讀掛載為 `/run/secrets/runtime.json`，由
`bootstrap.deployment_entrypoint` 驗證並載入允許的連線／憑證欄位。
請依交付的 role 範例與 loader 規則提供最小權限設定，不能將同一份 admin JSON
複製給每個服務。

Private JSON 最低必要欄位如下；`writer`／`reader` 各選完整 Basic pair 或單一 token：

| File | 連線／Identity 欄位 | MLflow 權限 |
| --- | --- | --- |
| `migrate.json` | `ML_DATABASE_URL` | 不提供 writer／reader |
| `management.json` | `ML_DATABASE_URL`、`APP_MONGO_URI`、`APP_JWT_SECRET` | 不提供 writer／reader |
| `relay.json` | `ML_DATABASE_URL`、`ML_RABBITMQ_URL` | 不提供 writer／reader |
| `supervisor.json` | `ML_DATABASE_URL`、`ML_RABBITMQ_URL`、`ML_REDIS_URL` | writer |
| `control.json` | `ML_DATABASE_URL`、`APP_MONGO_URI`、`APP_JWT_SECRET` | writer + 獨立 reader |
| `gateway.json` | `ML_DATABASE_URL`、`APP_MONGO_URI`、`APP_JWT_SECRET` | 不提供 writer／reader |
| `ingestion-worker.json` | `ML_DATABASE_URL` | 不提供 writer／reader；OAuth 另掛載 |
| `operator.json` | `ML_DATABASE_URL`、`APP_MONGO_URI`、`APP_JWT_SECRET` | writer + 獨立 reader，只授予核准操作範圍 |

`ML_DATABASE_URL` 必須使用 `postgresql+psycopg://`，與統一應用映像內的
psycopg 3 driver 一致；未指定 driver 的 `postgresql://` 會被拒絕。

例如 control 的 token 方式，所有 `<...>` 都由秘密管理流程填入真實核准值：

```json
{
  "ML_DATABASE_URL": "postgresql+psycopg://<approved-user>:<url-encoded-password>@<approved-db-host>/<approved-db>",
  "APP_MONGO_URI": "mongodb://<approved-user>:<url-encoded-password>@<approved-mongo-host>/?authSource=<approved-auth-db>",
  "APP_JWT_SECRET": "<approved-shared-jwt-material-at-least-32-bytes>",
  "MLFLOW_TRACKING_TOKEN": "<writer-token-from-secret-manager>",
  "ML_SERVING_MLFLOW_TOKEN": "<different-readonly-token-from-secret-manager>"
}
```

Basic 方式改用 `MLFLOW_TRACKING_USERNAME`／`MLFLOW_TRACKING_PASSWORD`，reader
則為 `ML_SERVING_MLFLOW_USERNAME`／`ML_SERVING_MLFLOW_PASSWORD`；同一 identity
不混用 Basic 與 token。`APP_JWT_SECRET` 至少 32 bytes，相關 Identity roles 的
核准 signing material 必須一致。Connection URI 的 TLS／CA、帳號與 scope 由
對應服務 owner 核准，不能把示例 URI 當作已連通的值。

外部 MLflow 的 writer credentials 僅提供給 Supervisor、Controller 及已授權的
operator；Controller／operator 另有 `ML_SERVING_MLFLOW_*` 唯讀 credentials。
Serving 子程序只取得其專用憑證、核准 CA 與 endpoint，不繼承 writer credentials
或父程序 home。Basic username/password 與 token 應依實際服務選擇一種核准方式。
不要停用 TLS certificate verification 來繞過連線失敗。

Private JSON／OAuth secret 使用 runtime UID 擁有的 `0400` 或 `0600` regular files，
拒絕 symlink；runtime root 應由核准 runtime UID 擁有並可寫。設定、OAuth secret、
CA 與 Operator definitions 放在 runtime root 外，避免透過另一個 writable mount
改寫 readonly 輸入。先用秘密管理／主機權限流程準備目錄，不對既有資料盲目遞迴
`chown`，也不以 world-writable 權限解決讀取失敗。

## 3. 建立固定的操作入口與離線 preflight

以下 shell variables 是 operator 本次操作的路徑，不是新的 application settings。
將所有 example／`<...>` 內容替換成核准值；保留 private evidence directory。

```sh
umask 077
day1_env=/absolute/private/ml-system-production.env
day1_project=ml-prod-site-a
day1_evidence=/absolute/private/day1-evidence/release-id/attempt-001
day1_image='registry.example.org/team/ml-system@sha256:<approved-64-hex-digest>'
mkdir -p "$day1_evidence"
chmod 0700 "$day1_evidence"

ml_release_compose() {
  docker compose --env-file "$day1_env" -p "$day1_project" \
    -f compose.ml.external-mlflow.yaml "$@"
}

ml_release_compose --profile ops config --quiet
ml_release_compose --profile ops config --format json > "$day1_evidence/rendered-compose.json"
ml_release_compose --profile ops pull
docker image inspect --format '{{json .}}' "$day1_image" > "$day1_evidence/image-inspect.json"
python -m bootstrap.deployment_cli preflight \
  --compose-config "$day1_evidence/rendered-compose.json" \
  --image-inspect "$day1_evidence/image-inspect.json" \
  --output "$day1_evidence/preflight.json"
```

離線 metadata 包含 `ops` profile，讓 preflight 也能核對 operator；`pull` 不會啟動它。
在可信 release checkout 的受支援 Python 環境執行 preflight。`image-inspect.json`
必須取自實際 pull 的 image，不手工編造；Docker `.Id` 是 image config digest，
不等於 Registry RepoDigest。有效 Compose metadata 也應來自同一次設定，不手改
輸出讓檢查通過。

Preflight 比對 image／code／role／production／HTTPS／limits 等設定，**不連線**
MLflow 或 Databricks，不證明 image 簽章、來源真實性、服務權限或 production Go。
成功報告為 `CONFIGURATION_PREFLIGHT_PASSED`，不是 production acceptance。
`config`／`inspect`／preflight 非零或 evidence 不一致時停止，不啟動下一階段。
每次重試使用新的 attempt evidence 路徑；工具拒絕覆寫既有報告，保留失敗紀錄。

## 4. Backup 與單次資料庫 migration

DB owner 依核准的變更窗口完成 App DB 與相關 recovery set 備份，記錄 backup ID、
時間、restore 驗證與決策人。已有環境先停用新提交／對外流量，暫停需受影響的
Relay／Supervisor／Controller；確認舊 execution 的實體狀態與 schema 相容性。

```sh
if ml_release_compose run --rm --no-deps migrate > "$day1_evidence/migrate.log" 2>&1; then
  printf '%s\n' 0 > "$day1_evidence/migrate.exit-code"
else
  day1_migrate_status=$?
  printf '%s\n' "$day1_migrate_status" > "$day1_evidence/migrate.exit-code"
  exit "$day1_migrate_status"
fi
```

成功條件是 migration command exit `0`，且所需 schema／revision 與該 release
一致。保存本次 attempt 的 command exit code、logs 與 DB owner 的 revision 核對紀錄；
不要重用別次 release／attempt 的 receipt。`run --rm` 使用臨時一次性 container，
成功後會移除，因此後續 `ps -a` 不會有這次 migration container；這不是漏啟動。
不要靠反覆 restart 掩蓋 migration 失敗。
Migration 失敗先保留 logs／revision evidence，交 DB owner 決定恢復；不要任意
執行 schema downgrade 或在 production DB 上跑測試 suite。

## 5. 驗證外部 MLflow 的實際權限與 artifacts

在 MLflow owner 核准的 qualification experiment／registered-model namespace 執行：

```sh
ml_release_compose --profile ops run --rm --no-deps operator \
  python -m bootstrap.deployment_cli qualify-mlflow \
  --experiment-name '<approved-qualification-experiment>' \
  --registered-model-prefix '<approved-qualification-model-prefix>' \
  --output /app/var/release-qualification/day1-attempt-001/mlflow.json
```

此操作會建立並保留可追溯的 qualification run／model version／artifact，不能
對未核准的正式 namespace 執行。它不做模型 approval、deployment 或刪除。
驗證 writer 的 Run／Registry／artifact upload，並由獨立 reader credentials 驗證
Registry／artifact download、內容一致，及 reader 的 tracking／registry writes
確實被拒絕。用同一組 admin credentials 或沒有權限隔離的 server 不能當作
Serving 唯讀驗收成功。

確認 evidence 檔已產生、command exit `0`、狀態為 `MLFLOW_DEPENDENCY_QUALIFIED`、
TLS 正確及預期權限邊界通過。重試改用新的 attempt 目錄，不刪除／覆寫失敗報告。
`/health`、Management readiness 或可以建立 SDK client 都不能代替這項驗證。
外部 server 需支援這個 release 使用的 Artifact Proxy／Registry 契約；直接 S3
credentials 與任意 artifact URI 並不是這份 profile 的替代路徑。

## 6. 準備身份與 SourceDefinition 草稿

Startup 不自動建立管理帳號。使用既有核准帳號；若需新增，經身份管理流程授權後，
在 trusted operator container 執行 `manage.py create-admin`，密碼由 prompt 或 private
stdin 提供。依職責分開 scientist、reviewer、deployer、predictor 與 ingestion operator。
`ml.ingest`、`ml.ingestion.read`、`ml.ingestion.operate`、`ml.source.manage` 等 dot scopes
會映射到對應的 ML colon scopes；避免把所有權限交給一般 prediction account。

Operator 的 `run` 會執行 secret-loading entrypoint；不要以 `docker compose exec`
取代，因為 exec 不會重新執行該 entrypoint。

```sh
ml_release_compose --profile ops run --rm --no-deps operator \
  python -m bootstrap.ingestion_cli templates
```

將實際回傳的 template digest 填入待覆核的 SourceDefinition 草稿。
範例 schema 見 [Databricks runbook](../databricks-ingestion-runbook.md#啟用註冊與執行)。
本步先準備 definition 草稿，核對 tenant、source ID／version、固定 Delta revision、
schema 與 bounds，將 regular JSON file 放在 host `ML_OPERATOR_DEFINITIONS_DIR`。
待第 7 步 API 啟動後，第 8 步再登入取得實際 `user_id`、完成 actor grants 核准並
執行註冊；不要在尚未啟動的 API 查詢身份，或先用 username 代替 `user_id`。

## 7. 啟動完整 runtime roles

僅在第 4 步的同一個 release／attempt 已有 migration exit `0` 與 revision 核對證據，
且前述 dependency qualification 成功後，才執行下列啟動程序。這裡明確使用
`--no-deps`：migration 已由 operator 執行並移除，若直接使用一般 `up`，Compose
會依 `depends_on` 再啟動一個 `migrate` service。先建立 Controller container 的
network namespace，再啟動共用該 namespace 的 Gateway。

```sh
(
  set -e
  test "$(cat "$day1_evidence/migrate.exit-code")" = 0
  ml_release_compose up -d --no-deps management relay supervisor control ingestion-worker
  ml_release_compose up -d --no-deps gateway
  ml_release_compose ps -a
)
```

不要加 `--build`，也不要在缺少／失敗的 migration receipt 時繞過 dependency。
上述 receipt 檢查只核對已記錄的 exit code，不取代 DB revision 與 release 的人工
核對。`operator` 在 `ops` profile 下，只在有授權作業時用 `run --rm` 啟動。
Runtime acceptance 至少核對：

| Role | 預期狀態／證據 |
| --- | --- |
| `migrate` | 第 4 步保存的單次 exit `0`、logs／revision receipt；`--rm` 後不應有持續運行的 container |
| `management` | running、readiness `200`，DB／Identity 正常 |
| `relay` | running，Outbox 沒有持續增長／DEAD backlog |
| `supervisor` | running，連接正確 broker／queue，沒有重啟或 ownership 異常 |
| `control` | running，能對帳 execution／deployment，沒有持續 reconciliation 錯誤 |
| `gateway` | running、readiness `200`，對應 Controller network namespace |
| `ingestion-worker` | running、worker health 正常，能依 SQL Job 執行受控擷取 |

```sh
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
curl --fail --silent --show-error http://127.0.0.1:8001/health/ready
ml_release_compose --profile ops run --rm --no-deps operator \
  python -m bootstrap.ingestion_cli health
```

若修改 `ML_MANAGEMENT_PORT`／`ML_PREDICTION_PORT`，同步使用核准的 host ports；
再經實際 HTTPS ingress 驗證使用者路徑。這些 health checks 只代表各自檢查範圍：
Management 與 Gateway 主要檢查 App DB／Identity，Ingestion health 檢查設定／storage／schema。
它們不代表 Warehouse OAuth、MLflow write/read、資料完整性或模型品質已通過。
Gateway readiness 也不表示某個 model generation 已載入；仍需完成 ACTIVE 與實際 prediction。

## 8. 首筆真實 Databricks ingestion 與 digest 驗證

Management API 啟動並就緒後，使用核准帳號登入，再 GET `/api/v1/identity/me`。
將回傳 `user_id` 填入 definition 的 `allowed_actor_ids`，不是 username。
Reviewer 核對 actor 所屬 tenant、`ml.ingest` grant 與 definition 其餘固定內容後，
在 trusted operator container 完成註冊：

```sh
ml_release_compose --profile ops run --rm --no-deps operator \
  python -m bootstrap.ingestion_cli register-source \
  --definition-path /run/ml/operator-definitions/approved-source.json \
  --tenant-id '<approved-tenant>'
```

容器使用設定中的 runtime UID／GID。檔案須可讀、不可經 writable runtime mount
改寫，最大 64 KiB；相同 tenant／source ID／version 不可換成不同內容。保存 CLI
回傳的 source/version/configuration digest。Operator 註冊不查詢 Warehouse；OAuth
secret 僅掛載 Ingestion Worker，不掛載一般 API、Controller 或 operator。

註冊成功後，使用這個核准 tenant／actor 的 bearer token，經管理 API 提交第一筆
受控 Job：

```http
POST /api/v1/ingestion/jobs
Authorization: Bearer <approved-access-token>
Idempotency-Key: <recorded-day1-ingestion-key>
Content-Type: application/json

{"source_id":"<approved-source-id>","source_version":"<approved-source-version>"}
```

這是 HTTP request 格式，token 不寫入 shell history／PR。使用 private header file
或受信任 API client，將 response 保存到 private evidence directory。Source ID／
version 必須與剛才註冊的 definition 一致。`202` 只表示接受工作；沿 `status_url`
查詢，直到 `PUBLISHED`，再 GET `result_url`。

接受條件：source configuration digest／`source_revision` 的固定 Delta version、
record count、content digest、manifest digest 均符合獨立核准 fixture。API result
只提供 metadata，不提供任意 filesystem path，也不替 downstream reader 驗證 bytes。

由有權執行 trusted operator CLI 的人驗證實際 Raw Store；替換以下四個 argument：

```sh
ml_release_compose --profile ops run --rm --no-deps -T operator \
  python - '<approved-tenant>' '<published-job-id>' '<approved-content-sha256>' '<approved-record-count>' <<'PY'
import json
import sys
from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.ml import Actor

tenant, job_id, expected_digest, expected_records = sys.argv[1:]
components = build_ingestion(IngestionSettings.from_env())
try:
    actor = Actor(tenant, "day1-operator", frozenset({"ml:ingestion:operate"}))
    snapshot = components.service.result(actor, job_id)
    verified = components.store.verify(snapshot)
    assert verified.content_digest == expected_digest
    assert verified.record_count == int(expected_records)
    print(json.dumps({"job_id": job_id, "snapshot_id": verified.snapshot_id,
                      "content_digest": verified.content_digest,
                      "manifest_digest": verified.manifest_digest,
                      "record_count": verified.record_count,
                      "source_revision": verified.source_revision}))
finally:
    components.close()
PY
```

此作業使用 trusted operator 的 DB／storage 存取權，不能提供給一般 API 使用者。
保存輸出及 reviewer 對 Delta revision 的核對結果；不要將測得 SHA 當作「事先獨立
核准」的 expected SHA。失敗／取消／不確定 publication 先按
[Ingestion recovery](../databricks-ingestion-runbook.md#失敗對帳與維運) 對帳，不刪掉 evidence
來強迫重新取一份資料。

## 9. 核准 DatasetVersion 的 Training／Serving smoke

**Raw Snapshot 不會自動變成 ML DatasetVersion。** 本步使用事先已核准並註冊、
tenant／recipe／schema 相符的 DatasetVersion；缺少它時是 No-Go，不能把新取回的
Raw Snapshot ID 填進 Training API。

以 scientist 帳號提交 `POST /api/v1/training/jobs`，帶唯一、已記錄的 Idempotency-Key：

```json
{
  "spec": {
    "recipe": {"id": "<approved-recipe>", "version": "<approved-version>"},
    "dataset": {"id": "<approved-dataset>", "version": "<approved-version>"}
  }
}
```

固定步驟與成功條件：

1. 沿回傳 `status_url` 查詢至 `SUCCEEDED`；保存 Job／Attempt／model binding IDs，
   確認 recipe、DatasetVersion、code/image、manifest／evaluation provenance。
2. GET `/api/v1/model_registry/bindings/{binding_id}/evaluations`；reviewer 核對固定
   evaluation／dataset／manifest digest，確認已核准的品質政策通過。
3. Reviewer 以自己的帳號 POST `/api/v1/model_registry/bindings/{binding_id}/approvals`，
   body 為 `{"evaluation_id":"<id>","environment":"prod"}`；必須是明確人工核准。
4. Deployer POST `/api/v1/deployments`，body 為
   `{"endpoint_id":"<approved-endpoint>","model_binding_id":"<binding-id>","approval_id":"<approval-id>"}`。
   GET `/api/v1/deployments/{deployment_id}`，等待 `ACTIVE`。
5. 對 Gateway POST `/api/v1/prediction/predict`，使用該 recipe 已核准的固定 input
   fixture，核對 prediction／schema 及 model version／deployment provenance。
6. 執行已核准的 rollback drill；POST `/api/v1/deployments/{deployment_id}/rollback`
   會建立新的 generation。核對路由、預測與授權；不要以 SQL 改寫 ACTIVE／成功狀態。

Quality rejection、未核定 EBM policy 或 golden mismatch 都不能跳過 approval。
不要在正式租戶隨意執行 synthetic demo、測試 suite 或自動將 model alias 指向 latest。
另由 Broker／Redis owner 核對此 correlation 的 publication／delivery／ACK evidence、
queue backlog 與重送狀態。App DB 的 `SUCCEEDED` 是業務結果，不單獨證明 broker ACK
或 Redis technical snapshot 已完成；不要刪除訊息或直接修改 SQL 來製造成功。

## 10. Metrics、Go／No-Go 與失敗回復

以具 `ml.operate` 的核准帳號讀取 Management `/metrics`，驗證 scrape／alerts
與 request／job／attempt／run／deployment correlation。觀察 failed/retrying jobs、
Outbox age／DEAD、ownership／reconciliation budget、broker redelivery、Ingestion oldest
pending age、Serving readiness／latency／capacity；指標名稱與低基數規則見
[runtime runbook](../ml-runbook.md) 及 [Ingestion runbook](../databricks-ingestion-runbook.md)。

以下每項都需 reviewer、時間、release／environment、evidence reference，空白就是
未完成，不因 CI 綠燈或 health `200` 自動勾選：

- [ ] 已核准版本化 release artifact、實際 image RepoDigest／code revision 與來源證據。
- [ ] Production settings、role secrets、TLS、UID／storage permissions 與資源／concurrency 核准完成。
- [ ] PostgreSQL migration 成功，協調備份及可執行 restore 計畫已確認。
- [ ] 外部 MLflow Run／Registry／artifact writer／reader／權限拒絕驗證通過。
- [ ] Databricks OAuth／固定 Delta fixture／Raw Snapshot bytes digest 實測通過。
- [ ] Management、Relay、Supervisor、Controller、Gateway、Ingestion Worker 狀態符合預期。
- [ ] API／Gateway／Worker health、必要 metrics／alerts 及實際 ingress 路徑正常。
- [ ] 核准 actor 的首筆 ingestion Job 為 `PUBLISHED`，receipt 與獨立 expected evidence 一致。
- [ ] 核准 DatasetVersion 的 Training／Evaluation／人工 Approval／Deployment／Prediction smoke 通過。
- [ ] Rollback／故障回復／監控／值班與決策負責人已演練並簽認。

十項 evidence 完整後，由核准決策人作出 Go；任何必要步驟失敗、skipped、未知或
證據不一致都是 No-Go。新環境尚未提供外部 fixture／credentials 時，保持待驗收，
不能將文件、Compose parse 或本機替身當成已上線。

失敗回復順序：停止新提交／停止對外導流；保留 Job／Attempt／execution、model／
artifact／snapshot、routing 與 logs evidence；判定已提交的 side effect 與實體程序
狀態；依核准計畫回復上一組相容的 image／code／config 與資料。Schema 或 artifact
已變更時由 DB／MLflow owner 決定協調 restore，不能只更換 tag。

必要時在變更窗口停止本次角色：

```sh
ml_release_compose stop ingestion-worker relay supervisor control gateway management
```

先確認 in-flight 工作、Serving drain 與實體停止證據，再執行受控 rollback／restore。
Production 不使用 `down --volumes`，不刪除未確定的 publication／execution，也不
手改資料庫來宣告成功。更完整的 Day-2 recovery／backup 操作見
[ML runtime runbook](../ml-runbook.md)。
