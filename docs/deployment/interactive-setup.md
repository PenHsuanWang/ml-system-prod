# 互動式初始化設定

`ml-system init` 在 Terminal 引導填寫設定，產生既有 Compose profiles 可直接讀取的
環境檔、Databricks connections／sources JSON 與 production private role JSON。
設定產生後，再依對應手冊執行離線 preflight、依賴資格、migration、帳號與來源註冊。
檔案格式通過不等於連線、資料品質或 production 已取得資格。

## 安裝與入口

在 Linux／WSL2，以預計執行 runtime 的非 root 使用者操作。需要 Python 3.11／3.12、
Git 與本 repository 的 checkout；初始化本身只需 base dependencies，不需先裝 Torch、
NumPy、MLflow 或 Databricks SDK。部署時再依 README／Docker targets 安裝對應依賴。

```sh
python -m pip install -e .
ml-system init
```

也可使用模組入口或直接選擇 profile：

```sh
python -m bootstrap.init_cli --profile compose
ml-system init --profile external
ml-system init --project-dir /absolute/path/to/checkout
ml-system init --private-dir /absolute/private/ml-setup
```

`--project-dir` 預設目前目錄；`--private-dir` 預設該 checkout 的 `.local/ml-system`。
Checkout 內的 private root 僅允許 `.local/ml-system` 或其子目錄，因為 Git 與 Docker
build context 都已忽略它。其他 private roots 必須位於 checkout 外；如果屬於另一個
Git repository，仍須通過 untracked／ignored 檢查。

需要可隱藏輸入的 Terminal，不能用 pipe／redirect 餵入回答；密碼、Token 不接受 CLI
arguments。`--help` 可在非互動環境執行。選單不分大小寫，Enter 採用顯示的預設值；
隱藏欄位只提示可保留已存／新產生值，不顯示內容。

## Profile 與產物

| 選擇 | 詢問／產生內容 | 後續 Compose files |
| --- | --- | --- |
| `compose`，不啟用 ingestion | 自動產生或保留五組本地服務密碼、JWT、UID／GID；可填 OTLP HTTP collector URL；寫入 `.env.ml` | `compose.ml.yaml` |
| `compose`，File Batch | 另填 approved input directory，寫入 `.env.ml.ingestion` | 加 `compose.ml.ingestion.yaml`／`--profile ingestion` |
| `compose`，Databricks OAuth | 另填連線與 OAuth、sources；產生 JSON／secret file／`.env.ml.ingestion` | 加 ingestion 與 `compose.ml.databricks.yaml` |
| `compose`，Databricks Token | 同上，但 Token 寫入受保護環境檔、JSON 僅保存 env reference | 加 ingestion 與 `compose.ml.databricks-token.yaml` |
| `external` | 外部 PostgreSQL／RabbitMQ／Redis／Mongo、JWT、MLflow writer／reader、release／CA／paths／limits，以及 Databricks | 僅 `compose.ml.external-mlflow.yaml` |

本地 Compose 固定使用內建基礎服務，URL 由既有 YAML 提供，不另詢問無法生效的
外部 MLflow URL。外部 profile 不與本地 YAML 合併；它依既有 Day-1 profile 要求
Databricks 連線與設定 mount。Wizard 目前管理一個 Warehouse connection，可有多個
核准 source templates；遇到既有多連線設定會停止，保留原檔供 operator 手動管理。

預設 private tree 依所選功能產生，未使用的檔案不會新增：

```text
.env.ml                       # local profile
.env.ml.ingestion             # optional local ingestion
.env.ml.production            # external profile
.local/ml-system/
  databricks/
    connections.json
    sources.json
    oauth-secret              # OAuth only
  roles/                      # external only
    migrate.json
    management.json
    relay.json
    supervisor.json
    control.json
    gateway.json
    ingestion-worker.json
    operator.json
  ingestion-input/            # optional local approved input mount
  operator-definitions/       # external approved SourceDefinition mount
```

Runtime data 預設另在 `var/`；private configuration、approved inputs 及 operator
definitions 必須在共享 runtime root 外。初始化建立所需目錄，保留既有 runtime 資料。
成功畫面只列輸出路徑、狀態與下一步命令，不列出設定內容。

## 欄位 mapping 與驗證

| 引導欄位 | 產物／既有 runtime mapping |
| --- | --- |
| Workspace hostname／Warehouse HTTP path | `connections.json` 的 `server_hostname`／`http_path`；hostname 不含 `https://`，使用既有 AWS／Azure／GCP Databricks 網域及 Warehouse path 驗證 |
| Databricks OAuth client ID／secret | JSON 保存 `client_id` 與容器路徑 `client_secret_file=/run/secrets/databricks_client_secret`；secret 存在 private host file |
| Databricks Token | JSON 保存 `access_token_env=DATABRICKS_TOKEN`；實際 Token 在 `.env.ml.ingestion` 或 `.env.ml.production`，只傳給 ingestion worker |
| Host JSON／secret 路徑 | `.env` 中的 `ML_DATABRICKS_CONNECTIONS_CONFIG`／`ML_DATABRICKS_SOURCES_CONFIG`／`ML_DATABRICKS_CLIENT_SECRET_FILE`；Compose 另映射為容器的 `*_FILE` 與 secret mount |
| MLflow HTTPS endpoint／CA | `.env.ml.production` 的 `ML_EXTERNAL_MLFLOW_URI`／`ML_MLFLOW_CA_FILE`；URI 不含 credentials／query／fragment，CA 須為可讀 regular PEM file |
| MLflow writer／reader | 八個 role JSON 中允許的 `MLFLOW_TRACKING_*`／`ML_SERVING_MLFLOW_*`；各選完整 Basic pair 或 Token，writer／reader 身份必須不同 |
| PostgreSQL／RabbitMQ／Redis／Mongo URI | 各角色需要的 `ML_DATABASE_URL`／`ML_RABBITMQ_URL`／`ML_REDIS_URL`／`APP_MONGO_URI`；含憑證的 URI 使用隱藏輸入，須符合既有 scheme 與 host／port 規則 |
| JWT／Identity database | 私有角色中的 `APP_JWT_SECRET` 至少 32 bytes；public `.env` 的 `APP_MONGO_DATABASE` |
| Release／source | 外部 profile 必填 `repository@sha256:<64 hex>` 與核准 40-hex source SHA；`ML_IMAGE_DIGEST` 從該 image reference 取得，不產生假 pin |
| Paths／ports／resources／deadlines | 沿用 `.env.ml.production.example` 的 keys；可選逐欄檢視進階值，最終檢查 port、UID／GID、positive limits、heartbeat／stale 與 ACK timeout 關係 |

外部 profile 沿用已存在的 artifact proxy 契約，application 不另保存直接 S3 憑證。
所有數值預設是設定起點，不是經核准的容量或 SLO。EBM 品質政策可保留原值或載入
已核准 JSON；空值保持 unqualified，不自動加入 engineering permissive policy。
完整欄位／角色矩陣見[設定參考](../configuration-reference.md)及
[Day-1 手冊](day1-deployment-runbook.md)。

Databricks sources 可選保留、匯入核准 JSON、逐欄建立基本 template 或空設定。
基本建立流程詢問 template reference、catalog／schema／table、固定 Delta version
與 projection columns／types；需要 predicates 等進階設定時匯入既有格式。
所有來源均經既有 `parse_connections`／`parse_templates` 驗證，且 reference 必須相符。
空 `sources.json` 是合法的待設定狀態，畫面會提示尚無 extraction source。
來源 grant／SourceDefinition 註冊仍依 [Databricks 手冊](../databricks-ingestion-runbook.md)。

## 既有檔案、備份與權限

每個已存在的輸出檔都先詢問：

```text
overwrite / backup / cancel    [cancel]
```

`backup` 會保留原始 bytes，名稱為 `<filename>.bak-<UTC timestamp>-<unique id>`。
所有檔案的選擇完成後，還需回答 `Write these files now? (y/n) [n]`。
在確認前取消、EOF 或 Ctrl-C 都不寫入設定。

`.env` 更新保留非管理行、註解與多行值；管理欄位使用 Compose literal quoting，避免
Token 中的 `$`、`#`、引號或反斜線被改寫。既有管理欄位若使用未解析的 `$VAR`／
`${VAR}`，先停止並要求明確值，不執行 shell 或偷偷讀取環境來展開；非管理 references
維持原文。請把 `.env.ml*` 當 Compose env files 使用，不以 shell `source` 載入。

MLflow writer／reader 已有憑證時，選擇預設的 `keep` 會保留各角色原有的認證方式與憑證。
明確選擇 `basic` 或 `token` 會將指定的憑證套用到對應的所有角色，並移除舊認證欄位。
明確更改共同連線值才更新相關角色。Wizard 會再次檢查各角色的必要與允許欄位。
本地舊密碼若不符合原 Compose URI 的安全字元集合，Wizard 停止而不自行旋轉。

私有檔案與備份為 `0600`，新目錄為 `0700`；既有 private root 亦須為該使用者擁有的
`0700` 目錄。既有設定必須是該 UID 擁有、mode `0400`／`0600` 的 regular files。
Runtime UID／GID 必須與執行初始化的非 root 使用者一致；不自動 sudo／chown。
檔案保存明文，權限保護不等於加密，仍須按站台 secrets 管理政策保管及輪替。

Wizard 拒絕 tracked／未被 Git 忽略的私有輸出、symlink、hardlink、FIFO、路徑 traversal、
重複 env／JSON keys 與寫入前發生的檔案變動。目錄鎖防止兩個 setup writer 同時發布。
每個檔案使用 owner-only 暫存、fsync 與原子替換；一般寫入失敗會回復已發布的檔案。
還原檔與備份各自獨立，修改還原檔時備份仍保留原始內容。

**多檔案不是檔案系統交易。** Process kill／斷電／無法回復的 I/O failure 可能留下
`<filename>.init-old-*`／`.init-new-*`／`.init-backup-*`；它們仍是 private、Git-ignored 的檔案。
發現殘留時，新一次初始化會停止。先保留所有檔案，核對 old bytes、已發布設定與
備份，再由 operator 決定回復／清理；不要把不完整設定當作部署成功。

## 完成後與開發驗證

完成畫面提供所選組合的 `docker compose ... config --quiet` 命令。
只有 standalone Compose v2 的環境可改用 `docker-compose` 前綴。
依 [README](../../README.md#reference-development-stack) 或 Day-1 手冊繼續操作。
原有 `scripts/create_ml_env.py` 仍保留，適合只需要本地隨機憑證的非互動流程。
K8s 繼續使用[原有 profile helpers](../../deploy/k8s/README.md)。

開發者可在 base/dev 環境執行：

```sh
python scripts/check_init.py
python -m pytest tests/ml/init_setup -q
python -m scripts.verify_init_compose
```

最後一個命令需要 Compose v2，使用 disposable Git checkout、測試憑證與有效 CA 格式，
只做真實 Compose 離線 rendering；不存取 Docker daemon、不啟動服務。它涵蓋本地、
File、Databricks OAuth／Token 及外部 OAuth／Token 六種組合，以及 literal 特殊字元。
CI 另以隔離 base/dev venv 驗證 console entrypoint、隱藏輸入與故障回復。
