# 圖面與原始碼對照

基準 `master@8c9023b00828000287e169ac760afe57102d7353`。每條箭頭記錄起點、終點、語義與可定位的原始碼。群組索引補充同一操作跨檔案的實作與 ADR。

拓撲圖的框線標示所屬範圍，箭頭顏色與線型見各圖圖例。交叉線各自通往箭頭所指的元件。時序圖每一欄代表固定參與者，由上往下閱讀；條件式訊息僅適用於標示的分支。

## 圖面索引

### 01-system-context.svg

C4 L1。使用者、核准來源、外部服務與產品邊界。

[開啟獨立 SVG](diagrams/01-system-context.svg)

- **ML System EBM — product boundary**　Governed CPU ML platform。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `input-0` | Scientist → Governed CPU ML platform | HTTP / JWT | sync | [src/api/ml_http.py:50](../../src/api/ml_http.py#L50) · [E01](#e01)、[E05](#e05)、[E06](#e06) |
| `input-1` | Reviewer / deployment operator → Governed CPU ML platform | Approve / deploy | manual | [src/api/ml_http.py:103](../../src/api/ml_http.py#L103) · [E01](#e01)、[E05](#e05)、[E06](#e06) |
| `input-2` | Data / platform operator → Governed CPU ML platform | Explicit CLI | manual | [bootstrap/ingestion_cli.py:17](../../bootstrap/ingestion_cli.py#L17) · [E01](#e01)、[E05](#e05)、[E06](#e06) |
| `input-3` | Prediction client → Governed CPU ML platform | HTTP / JWT | sync | [bootstrap/ml_gateway.py:65](../../bootstrap/ml_gateway.py#L65) · [E01](#e01)、[E05](#e05)、[E06](#e06) |
| `integration-0` | Governed CPU ML platform → Approved batch sources | Batch read | data | [src/application/ingestion/supervisor.py:128](../../src/application/ingestion/supervisor.py#L128) · [E05](#e05) |
| `integration-1` | Governed CPU ML platform → MLflow + artifact storage | Artifact I/O | data | [src/infrastructure/mlflow/tracking.py:90](../../src/infrastructure/mlflow/tracking.py#L90) · [E04](#e04) |
| `integration-2` | Governed CPU ML platform → Identity and coordination stores | State I/O | sync | [compose.ml.yaml:3](../../compose.ml.yaml#L3) · [E02](#e02) |
| `integration-3` | Governed CPU ML platform → Optional OTLP collector | OTLP HTTP | data | [bootstrap/ml_telemetry.py:30](../../bootstrap/ml_telemetry.py#L30) · [E12](#e12) |

### 02-container-architecture.svg

C4 L2。以 local-process execution backend 與 MLflow 為例。訓練與 Serving 是按需啟動的角色，共享資源各有唯一節點。

[開啟獨立 SVG](diagrams/02-container-architecture.svg)

- **Governed ML platform**　Management API、MongoDB、Prediction Gateway、Serving runtime、Control、Application PostgreSQL、Ingestion worker、Outbox Relay、RabbitMQ、Training Supervisor、Training compute、Runtime filesystem、Redis。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `identity-management` | Management API → MongoDB | Authenticate the principal via the identity module | sync | [bootstrap/ml_api.py:83](../../bootstrap/ml_api.py#L83) · [E01](#e01) |
| `identity-gateway` | Prediction Gateway → MongoDB | Resolve the token and active session | sync | [bootstrap/ml_gateway.py:33](../../bootstrap/ml_gateway.py#L33) · [E07](#e07) |
| `identity-serving` | Serving runtime → MongoDB | Authenticate the forwarded bearer | sync | [bootstrap/ml_serving.py:50](../../bootstrap/ml_serving.py#L50) · [E07](#e07) |
| `serve-request` | Prediction Gateway → Serving runtime | HTTP readiness and prediction at the resolved address | sync | [bootstrap/ml_gateway.py:91](../../bootstrap/ml_gateway.py#L91) · [E07](#e07) |
| `ensure-serving` | Control → Serving runtime | Ensure or retire the generation's local process | launch | [src/infrastructure/deployment/local_serving.py:93](../../src/infrastructure/deployment/local_serving.py#L93) · [E06](#e06) |
| `admission` | Management API → Application PostgreSQL | Persist jobs, approvals and deployment requests | data | [src/infrastructure/persistence/ml_training.py:97](../../src/infrastructure/persistence/ml_training.py#L97) · [E03](#e03)、[E06](#e06) |
| `route` | Prediction Gateway → Application PostgreSQL | Read deployment and recheck the active route | observe | [bootstrap/ml_gateway.py:98](../../bootstrap/ml_gateway.py#L98) · [E07](#e07) |
| `reconcile` | Control → Application PostgreSQL | Poll, fence and update durable state | observe | [bootstrap/ml_cli.py:86](../../bootstrap/ml_cli.py#L86) · [E06](#e06) |
| `ingest-state` | Ingestion worker → Application PostgreSQL | Claim ingestion work and commit snapshot metadata | data | [src/application/ingestion/supervisor.py:222](../../src/application/ingestion/supervisor.py#L222) · [E05](#e05) |
| `lease` | Outbox Relay → Application PostgreSQL | Lease the outbox and record publication | data | [src/application/training/outbox.py:22](../../src/application/training/outbox.py#L22) · [E03](#e03) |
| `publish` | Outbox Relay → RabbitMQ | AMQP publish with confirmation | async | [src/infrastructure/messaging/ml_rabbitmq.py:43](../../src/infrastructure/messaging/ml_rabbitmq.py#L43) · [E03](#e03) |
| `delivery` | RabbitMQ → Training Supervisor | Deliver the training command | async | [bootstrap/ml_worker.py:97](../../bootstrap/ml_worker.py#L97) · [E03](#e03) |
| `ack` | Training Supervisor → RabbitMQ | ACK after the terminal snapshot succeeds | async | [src/application/training/delivery.py:42](../../src/application/training/delivery.py#L42) · [E03](#e03) |
| `attempt` | Training Supervisor → Application PostgreSQL | Claim, fence and finalize the attempt | data | [src/application/training/supervisor.py:44](../../src/application/training/supervisor.py#L44) · [E04](#e04) |
| `fit` | Training Supervisor → Training compute | Launch the prepared training child | launch | [src/application/training/supervisor.py:62](../../src/application/training/supervisor.py#L62) · [E04](#e04) |
| `raw` | Ingestion worker → Runtime filesystem | Publish the immutable raw payload | data | [src/application/ingestion/supervisor.py:212](../../src/application/ingestion/supervisor.py#L212) · [E05](#e05) |
| `fetch` | Ingestion worker → Approved source | Acquire data through the approved adapter | data | [src/application/ingestion/supervisor.py:148](../../src/application/ingestion/supervisor.py#L148) · [E05](#e05) |
| `fit-files` | Training compute → Runtime filesystem | Read dataset bytes and write execution output | data | [bootstrap/ml_training_runner.py:70](../../bootstrap/ml_training_runner.py#L70) · [E04](#e04) |
| `ack-snapshot` | Training Supervisor → Redis | Persist the technical terminal snapshot | data | [src/infrastructure/messaging/training_ack.py:15](../../src/infrastructure/messaging/training_ack.py#L15) · [E03](#e03) |
| `register` | Training Supervisor → MLflow | Log run artifacts and register the bundle | data | [src/infrastructure/mlflow/tracking.py:90](../../src/infrastructure/mlflow/tracking.py#L90) · [E04](#e04) |
| `materialize` | Serving runtime → MLflow | Fetch pinned bundle bytes on a startup cache miss | data | [src/infrastructure/serving/materializer.py:41](../../src/infrastructure/serving/materializer.py#L41) · [E07](#e07) |
| `track-metadata` | MLflow → Tracking PostgreSQL | Persist tracking metadata over PostgreSQL | data | [compose.ml.yaml:79](../../compose.ml.yaml#L79) · [E08](#e08) |
| `track-bytes` | MLflow → MinIO | Serve bundle artifacts via the S3 backend | data | [compose.ml.yaml:80](../../compose.ml.yaml#L80) · [E08](#e08) |

### 03-component-architecture.svg

C4 L3。單一 Management executable 中處理 training admission 的元件。自動 startup wiring、source-level type import 與 request-time method call 分別標記。

[開啟獨立 SVG](diagrams/03-component-architecture.svg)

- **Management API process**　Management composition root、Training HTTP router、Management identity、Domain types and rules、TrainingService、ApprovedRecipeResolver、SQLDatasetCatalog、Training use-case ports、SQLTrainingRepository。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `wire-router` | Management composition root → Training HTTP router | Mount routers and expose services through app.state | wire | [bootstrap/ml_api.py:92](../../bootstrap/ml_api.py#L92) · [E02](#e02) |
| `wire-service` | Management composition root → TrainingService | Construct TrainingService(training, recipes) | wire | [bootstrap/ml_wiring.py:81](../../bootstrap/ml_wiring.py#L81) · [E02](#e02) |
| `actor` | Training HTTP router → Management identity | Resolve the caller's bearer token | sync | [src/api/ml_http.py:34](../../src/api/ml_http.py#L34) · [E01](#e01) |
| `authenticate` | Management identity → MongoDB | Read active session and user through the repository | sync | [src/application/identity/service.py:141](../../src/application/identity/service.py#L141) · [E01](#e01) |
| `submit` | Training HTTP router → TrainingService | Call training.submit with actor, key and intent | sync | [src/api/ml_http.py:56](../../src/api/ml_http.py#L56) · [E01](#e01) |
| `domain-imports` | TrainingService → Domain types and rules | Import domain types, digests and terminal-state rules | dependency | [src/application/training/service.py:7](../../src/application/training/service.py#L7) · [E02](#e02) |
| `resolve` | TrainingService → ApprovedRecipeResolver | Dispatch recipes.resolve through the injected port | sync | [src/application/training/service.py:27](../../src/application/training/service.py#L27) · [E02](#e02) |
| `dataset-version` | ApprovedRecipeResolver → SQLDatasetCatalog | Resolve the approved dataset identity | sync | [src/infrastructure/datasets/ml_recipes.py:71](../../src/infrastructure/datasets/ml_recipes.py#L71) · [E02](#e02) |
| `catalog-read` | SQLDatasetCatalog → Application PostgreSQL | Read dataset metadata for admission | data | [src/infrastructure/persistence/ml_datasets.py:41](../../src/infrastructure/persistence/ml_datasets.py#L41) · [E02](#e02) |
| `persist` | TrainingService → SQLTrainingRepository | Call find_submission and submit on the concrete adapter | sync | [src/application/training/service.py:28](../../src/application/training/service.py#L28) · [E02](#e02) |
| `imports` | TrainingService → Training use-case ports | Import Protocols for constructor annotations | dependency | [src/application/training/service.py:5](../../src/application/training/service.py#L5) · [E02](#e02) |
| `transaction` | SQLTrainingRepository → Application PostgreSQL | Commit the job, attempt, idempotency, outbox and audit | data | [src/infrastructure/persistence/ml_training.py:97](../../src/infrastructure/persistence/ml_training.py#L97) · [E03](#e03) |

### 04-training-sequence.svg

時序。由同一個訓練請求追蹤 SQL、broker、worker 與 artifact 的成功及條件分支。Approval 與 deployment 各有人工入口。

[開啟獨立 SVG](diagrams/04-training-sequence.svg)

時間由上而下，表中訊息編號沿用圖面順序。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `msg-01` | Scientist → Management | POST /api/v1/training/jobs + Idempotency-Key | sync | [src/api/ml_http.py:52](../../src/api/ml_http.py#L52) · [E01](#e01) |
| `msg-02` | Management → SQL | Commit job, attempt, key, outbox and audit together | data | [src/infrastructure/persistence/ml_training.py:97](../../src/infrastructure/persistence/ml_training.py#L97) · [E03](#e03) |
| `msg-03` | Management → Scientist | 202 · job_id and status_url | return | [src/api/ml_http.py:47](../../src/api/ml_http.py#L47) · [E01](#e01) |
| `msg-04` | Relay → SQL | Lease pending outbox row | observe | [src/application/training/outbox.py:22](../../src/application/training/outbox.py#L22) · [E03](#e03) |
| `msg-05` | Relay → RabbitMQ | Publish persistent command_id | async | [src/infrastructure/messaging/ml_rabbitmq.py:45](../../src/infrastructure/messaging/ml_rabbitmq.py#L45) · [E03](#e03) |
| `msg-06` | Relay → SQL | If publish fails → retry with backoff or mark DEAD | branch | [src/infrastructure/persistence/ml_outbox.py:55](../../src/infrastructure/persistence/ml_outbox.py#L55) · [E03](#e03) |
| `msg-07` | RabbitMQ → Relay | On success · publisher confirm | return | [src/infrastructure/messaging/ml_rabbitmq.py:47](../../src/infrastructure/messaging/ml_rabbitmq.py#L47) · [E03](#e03) |
| `msg-08` | Relay → SQL | Mark leased command PUBLISHED | data | [src/application/training/outbox.py:30](../../src/application/training/outbox.py#L30) · [E03](#e03) |
| `msg-09` | RabbitMQ → Supervisor | Deliver command · at least once | async | [bootstrap/ml_worker.py:97](../../bootstrap/ml_worker.py#L97) · [E03](#e03) |
| `msg-10` | Supervisor → SQL | Match durable intent and claim attempt | sync | [src/infrastructure/persistence/ml_training.py:155](../../src/infrastructure/persistence/ml_training.py#L155) · [E03](#e03) |
| `msg-11` | SQL → Supervisor | If terminal → skip fit and go to ACK gate | branch | [src/application/training/supervisor.py:45](../../src/application/training/supervisor.py#L45) · [E03](#e03) |
| `msg-12` | Supervisor → SQL | Persist prepared launch identity in execution_ref | data | [src/application/training/supervisor.py:60](../../src/application/training/supervisor.py#L60) · [E04](#e04) |
| `msg-13` | Supervisor → Compute | Spawn approved training process | launch | [src/infrastructure/execution/local_process.py:57](../../src/infrastructure/execution/local_process.py#L57) · [E04](#e04) |
| `msg-14` | Supervisor → SQL | If physical stop is uncertain → record reconciliation state | branch | [src/infrastructure/persistence/ml_training.py:314](../../src/infrastructure/persistence/ml_training.py#L314) · [E04](#e04) |
| `msg-15` | Compute → Compute | Fit and write frozen bundle, metrics and result.json | data | [src/plugins/training/pytorch_trainer.py:148](../../src/plugins/training/pytorch_trainer.py#L148) · [E04](#e04) |
| `msg-16` | Compute → Supervisor | After exit · result read from execution directory | return | [src/infrastructure/execution/local_process.py:117](../../src/infrastructure/execution/local_process.py#L117) · [E04](#e04) |
| `msg-17` | Supervisor → MLflow | Log metrics and register frozen artifact | data | [src/infrastructure/mlflow/tracking.py:90](../../src/infrastructure/mlflow/tracking.py#L90) · [E04](#e04) |
| `msg-18` | MLflow → Supervisor | Registry version and artifact_ref | return | [src/application/training/supervisor.py:180](../../src/application/training/supervisor.py#L180) · [E04](#e04) |
| `msg-19` | Supervisor → SQL | Save immutable ModelBinding | data | [src/application/training/supervisor.py:184](../../src/application/training/supervisor.py#L184) · [E04](#e04) |
| `msg-20` | Supervisor → Supervisor | Evaluate exact artifact on held-out data | sync | [src/application/training/supervisor.py:189](../../src/application/training/supervisor.py#L189) · [E04](#e04) |
| `msg-21` | Supervisor → SQL | If evaluator raises → save ERROR; leave recovery pending | branch | [src/application/training/supervisor.py:200](../../src/application/training/supervisor.py#L200) · [E04](#e04) |
| `msg-22` | Supervisor → SQL | Save Evaluation, then finish job with evidence IDs | data | [src/application/training/supervisor.py:208](../../src/application/training/supervisor.py#L208) · [E04](#e04) |
| `msg-23` | Supervisor → SQL | Validate exact command and terminal job | observe | [src/application/training/delivery.py:35](../../src/application/training/delivery.py#L35) · [E03](#e03) |
| `msg-24` | Supervisor → Redis | Persist technical snapshot with TTL | data | [src/infrastructure/messaging/training_ack.py:16](../../src/infrastructure/messaging/training_ack.py#L16) · [E03](#e03) |
| `msg-25` | Redis → Supervisor | If write fails → leave unacked; retry gate | branch | [bootstrap/ml_worker.py:81](../../bootstrap/ml_worker.py#L81) · [E03](#e03) |
| `msg-26` | Supervisor → RabbitMQ | After snapshot succeeds · consumer ACK | async | [src/application/training/delivery.py:42](../../src/application/training/delivery.py#L42) · [E03](#e03) |
| `msg-27` | Reviewer → Management | Submit Approval for evaluation and environment | manual | [src/api/ml_http.py:106](../../src/api/ml_http.py#L106) · [E06](#e06) |
| `msg-28` | Management → SQL | Check matching policy pass; save Approval | data | [src/application/model_registry/service.py:33](../../src/application/model_registry/service.py#L33) · [E06](#e06) |
| `msg-29` | Management → Reviewer | 201 · Approval | return | [src/api/ml_http.py:103](../../src/api/ml_http.py#L103) · [E06](#e06) |
| `msg-30` | Deployer → Management | Submit separate Deployment request | manual | [src/api/ml_http.py:121](../../src/api/ml_http.py#L121) · [E06](#e06) |
| `msg-31` | Management → SQL | Check approval; create REQUESTED generation | data | [src/infrastructure/persistence/ml_deployments.py:82](../../src/infrastructure/persistence/ml_deployments.py#L82) · [E06](#e06) |
| `msg-32` | Management → Deployer | 202 · deployment_id and generation | return | [src/api/ml_http.py:119](../../src/api/ml_http.py#L119) · [E06](#e06) |
| `msg-33` | Control → SQL | Poll deployment records for reconciliation | observe | [bootstrap/ml_cli.py:75](../../bootstrap/ml_cli.py#L75) · [E06](#e06) |
| `msg-34` | Control → Serving | Launch fixed generation under exclusive intent | launch | [src/infrastructure/deployment/local_serving.py:93](../../src/infrastructure/deployment/local_serving.py#L93) · [E06](#e06) |
| `msg-35` | Control → Serving | GET /health/ready and verify generation identity | sync | [src/infrastructure/deployment/local_serving.py:110](../../src/infrastructure/deployment/local_serving.py#L110) · [E06](#e06) |
| `msg-36` | Control → SQL | When ready, activate only at expected route revision | data | [src/infrastructure/persistence/ml_deployments.py:120](../../src/infrastructure/persistence/ml_deployments.py#L120) · [E06](#e06) |

### 05-inference-sequence.svg

時序。Gateway 與 Serving 分別驗證身份。依序檢查 readiness、成功回應的 provenance、最後一次 SQL route。

[開啟獨立 SVG](diagrams/05-inference-sequence.svg)

時間由上而下，表中訊息編號沿用圖面順序。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `msg-01` | Client → Gateway | POST /api/v1/prediction/predict + bearer | sync | [bootstrap/ml_gateway.py:65](../../bootstrap/ml_gateway.py#L65) · [E07](#e07) |
| `msg-02` | Gateway → MongoDB | Read current session and user permissions | sync | [src/infrastructure/ml_identity.py:44](../../src/infrastructure/ml_identity.py#L44) · [E07](#e07) |
| `msg-03` | Gateway → SQL | After ml:predict check, read tenant / endpoint route | observe | [bootstrap/ml_gateway.py:73](../../bootstrap/ml_gateway.py#L73) · [E07](#e07) |
| `msg-04` | Gateway → Client | If no active deployment → 503 | branch | [bootstrap/ml_gateway.py:76](../../bootstrap/ml_gateway.py#L76) · [E07](#e07) |
| `msg-05` | Gateway → SQL | Fetch deployment for route.deployment_id | sync | [bootstrap/ml_gateway.py:79](../../bootstrap/ml_gateway.py#L79) · [E07](#e07) |
| `msg-06` | Gateway → Gateway | Check generation and release state; resolve address | sync | [bootstrap/ml_gateway.py:83](../../bootstrap/ml_gateway.py#L83) · [E07](#e07) |
| `msg-07` | Gateway → Serving | GET /health/ready | sync | [bootstrap/ml_gateway.py:84](../../bootstrap/ml_gateway.py#L84) · [E07](#e07) |
| `msg-08` | Serving → Gateway | Readiness, tenant, deployment identity and Pod UID | return | [bootstrap/ml_serving.py:93](../../bootstrap/ml_serving.py#L93) · [E07](#e07) |
| `msg-09` | Gateway → Client | If unready or identity differs → 503 | branch | [bootstrap/ml_gateway.py:89](../../bootstrap/ml_gateway.py#L89) · [E07](#e07) |
| `msg-10` | Gateway → Serving | Forward prediction, bearer and trace context | sync | [bootstrap/ml_gateway.py:92](../../bootstrap/ml_gateway.py#L92) · [E07](#e07) |
| `msg-11` | Serving → MongoDB | Revalidate current session and user | sync | [src/api/ml_http.py:34](../../src/api/ml_http.py#L34) · [E07](#e07) |
| `msg-12` | Serving → Model runtime | Predict after scope, endpoint and input checks | sync | [bootstrap/ml_serving.py:111](../../bootstrap/ml_serving.py#L111) · [E07](#e07) |
| `msg-13` | Model runtime → Serving | Prediction values for the batch | return | [src/infrastructure/serving/bounded_prediction.py:44](../../src/infrastructure/serving/bounded_prediction.py#L44) · [E07](#e07) |
| `msg-14` | Serving → Gateway | Predictions with immutable deployment provenance | return | [bootstrap/ml_serving.py:115](../../bootstrap/ml_serving.py#L115) · [E07](#e07) |
| `msg-15` | Gateway → Gateway | For 2xx, match provenance and readiness Pod UID | sync | [bootstrap/ml_gateway.py:96](../../bootstrap/ml_gateway.py#L96) · [E07](#e07) |
| `msg-16` | Gateway → Client | If success identity differs → 503 | branch | [bootstrap/ml_gateway.py:97](../../bootstrap/ml_gateway.py#L97) · [E07](#e07) |
| `msg-17` | Gateway → SQL | Read the same tenant / endpoint route again | observe | [bootstrap/ml_gateway.py:98](../../bootstrap/ml_gateway.py#L98) · [E07](#e07) |
| `msg-18` | SQL → Gateway | Current route including revision and generation | return | [src/infrastructure/persistence/ml_deployments.py:110](../../src/infrastructure/persistence/ml_deployments.py#L110) · [E07](#e07) |
| `msg-19` | Gateway → Client | If route changed or response redirects → 503 | branch | [bootstrap/ml_gateway.py:100](../../bootstrap/ml_gateway.py#L100) · [E07](#e07) |
| `msg-20` | Gateway → Client | Stable route · return Serving body and status | return | [bootstrap/ml_gateway.py:102](../../bootstrap/ml_gateway.py#L102) · [E07](#e07) |

### 06-local-runtime.svg

Native。上方是 CLI demo，下方是同一 Linux 主機的多程序組合與須可達的依賴。完整 native macOS 行為仍待驗證。

[開啟獨立 SVG](diagrams/06-local-runtime.svg)

- **Training demo  /  one host**　ml_cli demo  /  demo-ebm、Compute child、Execution directory、SQLite + local tracker files。
- **HTTP and worker roles  /  one host**　Prediction Gateway、Serving child、Control、Management API、Outbox Relay、Training Supervisor、Compute child、Optional ingestion worker、Runtime files、Migration CLI。
- **Reachable dependencies**　Application SQL、MongoDB、RabbitMQ、Redis、MLflow。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `demo-launch` | ml_cli demo  /  demo-ebm → Compute child | Launch one OS process per accepted attempt | launch | [src/infrastructure/execution/local_process.py:57](../../src/infrastructure/execution/local_process.py#L57) · [E04](#e04) |
| `demo-output` | Compute child → Execution directory | Write checkpoints, metrics and frozen bundle | data | [src/plugins/training/pytorch_trainer.py:148](../../src/plugins/training/pytorch_trainer.py#L148) · [E04](#e04) |
| `demo-persist` | ml_cli demo  /  demo-ebm → SQLite + local tracker files | Migrate, persist job, lease command directly and register model evidence | data | [bootstrap/ml_cli.py:145](../../bootstrap/ml_cli.py#L145) · [E09](#e09) |
| `predict` | Prediction Gateway → Serving child | Forward to the fixed process over loopback HTTP | sync | [bootstrap/ml_gateway.py:91](../../bootstrap/ml_gateway.py#L91) · [E07](#e07) |
| `serve-launch` | Control → Serving child | Start a fixed-model Serving child | launch | [src/infrastructure/deployment/local_serving.py:93](../../src/infrastructure/deployment/local_serving.py#L93) · [E07](#e07) |
| `admit` | Management API → Application SQL | Persist admitted jobs and governance state | data | [src/application/training/service.py:28](../../src/application/training/service.py#L28) · [E03](#e03) |
| `authenticate` | Management API → MongoDB | Resolve tenant and identity | sync | [bootstrap/identity.py:23](../../bootstrap/identity.py#L23) · [E01](#e01) |
| `publish` | Outbox Relay → RabbitMQ | Publish a leased command with broker confirmation | async | [src/infrastructure/messaging/ml_rabbitmq.py:43](../../src/infrastructure/messaging/ml_rabbitmq.py#L43) · [E03](#e03) |
| `consume` | RabbitMQ → Training Supervisor | Deliver to the Supervisor for manual acknowledgement | async | [bootstrap/ml_worker.py:69](../../bootstrap/ml_worker.py#L69) · [E03](#e03) |
| `snapshot` | Training Supervisor → Redis | Store terminal technical result before AMQP acknowledgement | data | [bootstrap/ml_worker.py:50](../../bootstrap/ml_worker.py#L50) · [E03](#e03) |
| `register` | Training Supervisor → MLflow | Register the frozen model bundle using the selected tracker | data | [src/application/training/supervisor.py:180](../../src/application/training/supervisor.py#L180) · [E04](#e04) |
| `train-launch` | Training Supervisor → Compute child | Start isolated training compute | launch | [src/infrastructure/execution/local_process.py:57](../../src/infrastructure/execution/local_process.py#L57) · [E04](#e04) |
| `train-files` | Compute child → Runtime files | Read datasets and write attempt output | data | [src/plugins/training/pytorch_trainer.py:148](../../src/plugins/training/pytorch_trainer.py#L148) · [E04](#e04) |
| `publish-raw` | Optional ingestion worker → Runtime files | Publish immutable raw snapshots | data | [src/application/ingestion/supervisor.py:212](../../src/application/ingestion/supervisor.py#L212) · [E05](#e05) |
| `schema` | Migration CLI → Application SQL | Prepare SQL schema before starting the full roles | data | [bootstrap/ml_cli.py:33](../../bootstrap/ml_cli.py#L33) · [E09](#e09) |

### 07-docker-compose-runtime.svg

Compose。框線標示 project network、Docker container 與共享 network namespace。以 compose.ml.yaml 為主，疊加角色明確標記。

[開啟獨立 SVG](diagrams/07-docker-compose-runtime.svg)

- **Compose project  /  ml-system-ebm**　management、postgres  /  :5432、mongo  /  :27017、migrate  /  one-shot、ingestion-worker  /  profile、relay、rabbitmq  /  :5672、redis  /  :6379、mlflow  /  :5000、mlflow-db  /  :5432、minio  /  :9000、minio-init  /  one-shot、supervisor  /  Docker container、Control network namespace。
- **supervisor  /  Docker container**　Taskiq Supervisor、Compute child。
- **Control network namespace**　control  /  Docker container、gateway  /  Docker container。
- **control  /  Docker container**　Control、Serving child。
- **gateway  /  Docker container**　Gateway。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `management-sql` | management → postgres  /  :5432 | Persist application metadata in PostgreSQL | data | [compose.ml.yaml:4](../../compose.ml.yaml#L4) · [E08](#e08) |
| `management-auth` | management → mongo  /  :27017 | Resolve identity using MongoDB | sync | [compose.ml.yaml:18](../../compose.ml.yaml#L18) · [E01](#e01) |
| `migration` | migrate  /  one-shot → postgres  /  :5432 | Apply schema migrations to the application database | data | [compose.ml.yaml:94](../../compose.ml.yaml#L94) · [E08](#e08) |
| `ingest-start` | migrate  /  one-shot → ingestion-worker  /  profile | Successful migration gates ingestion overlay startup | dependency | [compose.ml.ingestion.yaml:24](../../compose.ml.ingestion.yaml#L24) · [E08](#e08) |
| `publish` | relay → rabbitmq  /  :5672 | Publish training command over AMQP | async | [src/infrastructure/messaging/ml_rabbitmq.py:43](../../src/infrastructure/messaging/ml_rabbitmq.py#L43) · [E03](#e03) |
| `consume` | rabbitmq  /  :5672 → Taskiq Supervisor | Deliver command to the manual-ACK consumer | async | [bootstrap/ml_worker.py:69](../../bootstrap/ml_worker.py#L69) · [E03](#e03) |
| `fork` | Taskiq Supervisor → Compute child | Spawn the training runner inside the Supervisor container | launch | [src/infrastructure/execution/local_process.py:57](../../src/infrastructure/execution/local_process.py#L57) · [E04](#e04) |
| `result` | Taskiq Supervisor → redis  /  :6379 | Store technical terminal result before acknowledging delivery | data | [bootstrap/ml_worker.py:50](../../bootstrap/ml_worker.py#L50) · [E03](#e03) |
| `registration` | Taskiq Supervisor → mlflow  /  :5000 | Register the frozen bundle through MLflow | data | [src/application/training/supervisor.py:180](../../src/application/training/supervisor.py#L180) · [E04](#e04) |
| `train-files` | Compute child → Host bind mount | Write execution outputs through the application bind mount | data | [compose.ml.yaml:29](../../compose.ml.yaml#L29) · [E08](#e08) |
| `serving-spawn` | Control → Serving child | Launch Serving child in the Control container | launch | [src/infrastructure/deployment/local_serving.py:93](../../src/infrastructure/deployment/local_serving.py#L93) · [E07](#e07) |
| `forward` | Gateway → Serving child | Reach Serving over shared network namespace loopback | sync | [compose.ml.yaml:131](../../compose.ml.yaml#L131) · [E07](#e07) |
| `mlflow-db` | mlflow  /  :5000 → mlflow-db  /  :5432 | Store tracking and registry metadata in PostgreSQL | data | [compose.ml.yaml:79](../../compose.ml.yaml#L79) · [E08](#e08) |
| `mlflow-s3` | mlflow  /  :5000 → minio  /  :9000 | Proxy model artifacts to the MinIO S3 endpoint | data | [compose.ml.yaml:80](../../compose.ml.yaml#L80) · [E08](#e08) |
| `make-bucket` | minio-init  /  one-shot → minio  /  :9000 | Create ml-artifacts bucket before MLflow starts | data | [compose.ml.yaml:75](../../compose.ml.yaml#L75) · [E08](#e08) |

### 08-kubernetes-runtime.svg

Kubernetes。觀察指定 Minikube overlay 的 Pod 與 container 邊界。Serving 資源依 Deployment identity 建立；資格限制見第 8 章。

[開啟獨立 SVG](diagrams/08-kubernetes-runtime.svg)

- **Application qualification namespace**　Service · ml-management、Pod · mongodb、Pod · postgres、Pod · rabbitmq、Pod · redis、Service · ml-gateway、Service · ml-serve-<UUID>、Immutable ConfigMap、PVC · ml-runtime、Pod · minio、Job Pod · minio-init、ml-management  /  Pod、ml-migrate  /  Job Pod、ml-ingestion-worker  /  Pod、ml-relay  /  Pod、ml-supervisor  /  Pod、ml-control  /  Pod、ml-serve-<UUID>  /  Kubernetes Deployment、mlflow  /  Pod。
- **ml-management  /  Pod**　management。
- **ml-migrate  /  Job Pod**　migrate。
- **ml-ingestion-worker  /  Pod**　ingestion-worker。
- **ml-relay  /  Pod**　relay。
- **ml-supervisor  /  Pod**　supervisor  /  container。
- **supervisor  /  container**　Taskiq Supervisor、Guardian、Compute child。
- **ml-control  /  Pod**　gateway、control。
- **ml-serve-<UUID>  /  Kubernetes Deployment**　Serving replica  /  Pod。
- **Serving replica  /  Pod**　serving、Bounded emptyDir。
- **mlflow  /  Pod**　tls、mlflow。

| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |
| --- | --- | --- | --- | --- |
| `ingress-mgmt` | Service · ml-management → management | Forward HTTP requests to the selected Management Pod | sync | [deploy/k8s/base/services.yaml:12](../../deploy/k8s/base/services.yaml#L12) · [E11](#e11) |
| `identity` | management → Pod · mongodb | Read tenant identity and sessions using MongoDB | sync | [bootstrap/identity.py:23](../../bootstrap/identity.py#L23) · [E01](#e01) |
| `app-sql` | management → Pod · postgres | Use application database in PostgreSQL | data | [deploy/k8s/examples/runtime-config.example.json:2](../../deploy/k8s/examples/runtime-config.example.json#L2) · [E11](#e11) |
| `migrate-sql` | migrate → Pod · postgres | Apply the application schema | data | [bootstrap/ml_cli.py:33](../../bootstrap/ml_cli.py#L33) · [E11](#e11) |
| `poll-ingestion` | ingestion-worker → Pod · postgres | Poll SQL for ingestion jobs independently of the training queue | observe | [src/application/ingestion/supervisor.py:128](../../src/application/ingestion/supervisor.py#L128) · [E05](#e05) |
| `publish` | relay → Pod · rabbitmq | Publish command to RabbitMQ Service | async | [src/infrastructure/messaging/ml_rabbitmq.py:43](../../src/infrastructure/messaging/ml_rabbitmq.py#L43) · [E03](#e03) |
| `receive` | Pod · rabbitmq → Taskiq Supervisor | Deliver command over AMQP to the Supervisor | async | [bootstrap/ml_worker.py:69](../../bootstrap/ml_worker.py#L69) · [E03](#e03) |
| `start-guardian` | Taskiq Supervisor → Guardian | Start the Pod-aware guardian inside the Supervisor container | launch | [src/infrastructure/execution/pod_process.py:142](../../src/infrastructure/execution/pod_process.py#L142) · [E11](#e11) |
| `start-runner` | Guardian → Compute child | Launch the runner in a new process group | launch | [bootstrap/ml_training_guardian.py:72](../../bootstrap/ml_training_guardian.py#L72) · [E11](#e11) |
| `snapshot` | Taskiq Supervisor → Pod · redis | Store the terminal technical snapshot before manual ACK | data | [bootstrap/ml_worker.py:50](../../bootstrap/ml_worker.py#L50) · [E03](#e03) |
| `gateway-ingress` | Service · ml-gateway → gateway | Forward requests to Gateway in the selected Control Pod | sync | [deploy/k8s/base/services.yaml:20](../../deploy/k8s/base/services.yaml#L20) · [E11](#e11) |
| `forward` | gateway → Service · ml-serve-<UUID> | Resolve the deployment Service name and forward prediction | sync | [src/infrastructure/deployment/endpoints.py:26](../../src/infrastructure/deployment/endpoints.py#L26) · [E07](#e07) |
| `serve-route` | Service · ml-serve-<UUID> → serving | Route to the ready replica on port 8001 | sync | [src/infrastructure/deployment/kubernetes_serving.py:253](../../src/infrastructure/deployment/kubernetes_serving.py#L253) · [E11](#e11) |
| `ensure-svc` | control → Service · ml-serve-<UUID> | Ensure or retire the per-deployment Service via Kubernetes API | observe | [src/infrastructure/deployment/kubernetes_serving.py:293](../../src/infrastructure/deployment/kubernetes_serving.py#L293) · [E11](#e11) |
| `ensure-spec` | control → Immutable ConfigMap | Ensure or retire the immutable spec ConfigMap via Kubernetes API | observe | [src/infrastructure/deployment/kubernetes_serving.py:296](../../src/infrastructure/deployment/kubernetes_serving.py#L296) · [E11](#e11) |
| `read-spec` | serving → Immutable ConfigMap | Read the fixed spec through its read-only volume mount | data | [src/infrastructure/deployment/kubernetes_serving.py:178](../../src/infrastructure/deployment/kubernetes_serving.py#L178) · [E11](#e11) |
| `cache-fill` | serving → Bounded emptyDir | Materialize and verify the startup bundle in Pod-local storage | data | [bootstrap/ml_serving.py:54](../../bootstrap/ml_serving.py#L54) · [E11](#e11) |
| `observe-evidence` | control → PVC · ml-runtime | Read shared execution evidence during reconciliation | data | [src/infrastructure/execution/pod_process.py:167](../../src/infrastructure/execution/pod_process.py#L167) · [E11](#e11) |
| `ensure-deployment` | control → ml-serve-<UUID>  /  Kubernetes Deployment | Create, observe or retire the fixed-version Kubernetes Deployment via scoped API | observe | [src/infrastructure/deployment/kubernetes_serving.py:284](../../src/infrastructure/deployment/kubernetes_serving.py#L284) · [E11](#e11) |
| `checkpoint` | Compute child → PVC · ml-runtime | Write checkpoints and frozen output under the shared execution root | data | [src/plugins/training/pytorch_trainer.py:148](../../src/plugins/training/pytorch_trainer.py#L148) · [E11](#e11) |
| `model-read` | serving → tls | Read exact artifacts via the MLflow HTTPS Service at startup | data | [deploy/k8s/base/configmap.yaml:10](../../deploy/k8s/base/configmap.yaml#L10) · [E11](#e11) |
| `tls-upstream` | tls → mlflow | Proxy HTTPS traffic to MLflow on loopback HTTP :5000 | sync | [deploy/k8s/infrastructure/minikube/files/nginx.conf:18](../../deploy/k8s/infrastructure/minikube/files/nginx.conf#L18) · [E11](#e11) |
| `artifact-store` | mlflow → Pod · minio | Proxy model artifacts to the MinIO S3 endpoint | data | [deploy/k8s/infrastructure/minikube/mlflow.yaml:83](../../deploy/k8s/infrastructure/minikube/mlflow.yaml#L83) · [E11](#e11) |
| `create-bucket` | Job Pod · minio-init → Pod · minio | Initialize the ml-artifacts bucket | data | [deploy/k8s/infrastructure/minikube/minio-init-job.yaml:31](../../deploy/k8s/infrastructure/minikube/minio-init-job.yaml#L31) · [E11](#e11) |

## E01

**HTTP／identity／admission**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [bootstrap/ml_api.py:27](../../bootstrap/ml_api.py#L27) | `def create_app(` |
| [src/api/ml_http.py:51](../../src/api/ml_http.py#L51) | `def submit(` |
| [src/api/ml_http.py:159](../../src/api/ml_http.py#L159) | `def mount_management(` |
| [src/api/ingestion/routers.py:64](../../src/api/ingestion/routers.py#L64) | `def submit(` |
| [src/application/identity/service.py:33](../../src/application/identity/service.py#L33) | `class IdentityService:` |
| [src/api/identity/routers.py:23](../../src/api/identity/routers.py#L23) | `@router.post("/login"` |
| [src/infrastructure/ml_identity.py:16](../../src/infrastructure/ml_identity.py#L16) | `class MongoActorResolver` |
| [bootstrap/ml_api.py:82](../../bootstrap/ml_api.py#L82) | `service = app.state.runtime.modules["identity"]` |
| [src/infrastructure/persistence/identity.py:35](../../src/infrastructure/persistence/identity.py#L35) | `class MongoIdentityRepository:` |
| [src/api/ml_http.py:34](../../src/api/ml_http.py#L34) | `return await request.app.state.ml_identity.resolve` |
| [src/api/ml_http.py:56](../../src/api/ml_http.py#L56) | `return public_job(services.training.submit` |

## E02

**Composition／dependency direction**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [bootstrap/ml_wiring.py:61](../../bootstrap/ml_wiring.py#L61) | `def build_components(` |
| [bootstrap/ml_wiring.py:42](../../bootstrap/ml_wiring.py#L42) | `def supervisor(` |
| [bootstrap/ml_training_wiring.py:7](../../bootstrap/ml_training_wiring.py#L7) | `def training_executor(` |
| [bootstrap/ml_serving_wiring.py:11](../../bootstrap/ml_serving_wiring.py#L11) | `def endpoint_resolver(` |
| [bootstrap/components.py:57](../../bootstrap/components.py#L57) | `class ModuleDefinition:` |
| [bootstrap/components.py:80](../../bootstrap/components.py#L80) | `class ModuleCatalog:` |
| [bootstrap/wiring.py:9](../../bootstrap/wiring.py#L9) | `def build_modules(` |
| [src/application/training/ports.py:12](../../src/application/training/ports.py#L12) | `class TrainingRepository` |
| [src/domain/training/models.py:11](../../src/domain/training/models.py#L11) | `class JobState` |
| [src/domain/training/workloads.py:13](../../src/domain/training/workloads.py#L13) | `class WorkloadContract` |
| [bootstrap/ml_wiring.py:81](../../bootstrap/ml_wiring.py#L81) | `services = MLServices(TrainingService(training, recipes)` |
| [bootstrap/ml_api.py:92](../../bootstrap/ml_api.py#L92) | `current.state.ml_services = components.services` |
| [src/application/training/service.py:5](../../src/application/training/service.py#L5) | `from src.application.training.ports import RecipeResolver` |
| [src/application/training/service.py:7](../../src/application/training/service.py#L7) | `from src.domain.training.models import Attempt` |
| [src/application/training/service.py:27](../../src/application/training/service.py#L27) | `spec_json = self.recipes.resolve` |
| [src/application/training/service.py:28](../../src/application/training/service.py#L28) | `return self.repository.submit` |
| [src/infrastructure/datasets/ml_recipes.py:71](../../src/infrastructure/datasets/ml_recipes.py#L71) | `dataset = self.catalog.resolve(` |
| [src/infrastructure/persistence/ml_datasets.py:39](../../src/infrastructure/persistence/ml_datasets.py#L39) | `def resolve(self, actor: Actor, dataset_id: str, version: str)` |

## E03

**SQL authority／Outbox／delivery**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [src/application/training/service.py:15](../../src/application/training/service.py#L15) | `def submit(` |
| [src/infrastructure/persistence/ml_training.py:67](../../src/infrastructure/persistence/ml_training.py#L67) | `def submit(` |
| [src/infrastructure/persistence/ml_training.py:176](../../src/infrastructure/persistence/ml_training.py#L176) | `def validate_delivery(` |
| [src/infrastructure/persistence/ml_database.py:144](../../src/infrastructure/persistence/ml_database.py#L144) | `class Database:` |
| [src/application/training/outbox.py:15](../../src/application/training/outbox.py#L15) | `class OutboxRelay:` |
| [src/infrastructure/messaging/ml_rabbitmq.py:14](../../src/infrastructure/messaging/ml_rabbitmq.py#L14) | `class RabbitMQCommandPublisher` |
| [src/infrastructure/messaging/ml_topology.py:4](../../src/infrastructure/messaging/ml_topology.py#L4) | `def build_training_broker(` |
| [src/application/training/delivery.py:29](../../src/application/training/delivery.py#L29) | `async def acknowledge_terminal(` |
| [src/infrastructure/messaging/ml_rabbitmq.py:43](../../src/infrastructure/messaging/ml_rabbitmq.py#L43) | `confirmation = await self.exchange.publish` |
| [bootstrap/ml_worker.py:77](../../bootstrap/ml_worker.py#L77) | `state["acknowledgement"], context.ack` |
| [src/application/training/outbox.py:28](../../src/application/training/outbox.py#L28) | `await asyncio.to_thread(self.repository.retry, item)` |
| [src/infrastructure/persistence/ml_outbox.py:55](../../src/infrastructure/persistence/ml_outbox.py#L55) | `state="DEAD" if row["tries"] >= 20` |
| [src/infrastructure/messaging/ml_rabbitmq.py:47](../../src/infrastructure/messaging/ml_rabbitmq.py#L47) | `if not isinstance(confirmation, Basic.Ack)` |
| [src/infrastructure/messaging/training_ack.py:16](../../src/infrastructure/messaging/training_ack.py#L16) | `"ml:ack:" + snapshot.command_id` |
| [src/application/training/delivery.py:41](../../src/application/training/delivery.py#L41) | `await store.persist(snapshot)` |
| [src/application/training/delivery.py:42](../../src/application/training/delivery.py#L42) | `await ack()` |

## E04

**Compute／registration／evaluation／recovery**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [src/application/training/supervisor.py:41](../../src/application/training/supervisor.py#L41) | `def execute(` |
| [src/application/training/supervisor.py:71](../../src/application/training/supervisor.py#L71) | `def monitor(` |
| [src/application/training/supervisor.py:139](../../src/application/training/supervisor.py#L139) | `def finalize(` |
| [src/application/training/supervisor.py:163](../../src/application/training/supervisor.py#L163) | `def _finalize(` |
| [bootstrap/ml_worker.py:23](../../bootstrap/ml_worker.py#L23) | `def build_broker(` |
| [bootstrap/ml_training_runner.py:119](../../bootstrap/ml_training_runner.py#L119) | `def main(` |
| [src/infrastructure/execution/local_process.py:49](../../src/infrastructure/execution/local_process.py#L49) | `def launch(` |
| [src/infrastructure/mlflow/tracking.py:12](../../src/infrastructure/mlflow/tracking.py#L12) | `class LocalExperimentTracker` |
| [src/infrastructure/mlflow/tracking.py:46](../../src/infrastructure/mlflow/tracking.py#L46) | `class MLflowExperimentTracker` |
| [src/infrastructure/serving/evaluation.py:108](../../src/infrastructure/serving/evaluation.py#L108) | `class WorkloadBundleEvaluator` |
| [bootstrap/ml_training_runner.py:69](../../bootstrap/ml_training_runner.py#L69) | `dataset = FileDatasetSource(dataset_root).load` |
| [src/infrastructure/mlflow/tracking.py:90](../../src/infrastructure/mlflow/tracking.py#L90) | `self.client.log_artifacts(run_id` |
| [src/infrastructure/persistence/ml_training.py:210](../../src/infrastructure/persistence/ml_training.py#L210) | `def prepare_execution(` |
| [src/application/training/supervisor.py:60](../../src/application/training/supervisor.py#L60) | `self.repository.prepare_execution(claim,` |
| [src/application/training/supervisor.py:62](../../src/application/training/supervisor.py#L62) | `self.executor.launch(handle)` |
| [src/plugins/training/pytorch_trainer.py:148](../../src/plugins/training/pytorch_trainer.py#L148) | `atomic_json(directory/"result.json", result)` |
| [src/plugins/training/ebm_trainer_v1.py:244](../../src/plugins/training/ebm_trainer_v1.py#L244) | `atomic_json(directory / "result.json", result)` |
| [src/infrastructure/execution/local_process.py:117](../../src/infrastructure/execution/local_process.py#L117) | `Path(handle.directory)/"result.json"` |
| [src/application/training/supervisor.py:180](../../src/application/training/supervisor.py#L180) | `version, artifact_ref = self.tracker.register(` |
| [src/application/training/supervisor.py:184](../../src/application/training/supervisor.py#L184) | `binding = self.registry.save_binding(` |
| [src/application/training/supervisor.py:189](../../src/application/training/supervisor.py#L189) | `evaluation = self.evaluator.evaluate(` |
| [src/application/training/supervisor.py:202](../../src/application/training/supervisor.py#L202) | `evaluation = self.registry.save_evaluation(evaluation)` |
| [src/application/training/supervisor.py:208](../../src/application/training/supervisor.py#L208) | `self.repository.finish(claim, binding.binding_id, evaluation.evaluation_id)` |

## E05

**Ingestion／RawSnapshot／DatasetBuilder**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [src/application/ingestion/service.py:20](../../src/application/ingestion/service.py#L20) | `def submit(` |
| [src/application/ingestion/supervisor.py:128](../../src/application/ingestion/supervisor.py#L128) | `def run_once(` |
| [src/application/datasets/builder.py:72](../../src/application/datasets/builder.py#L72) | `class EBMDatasetPreparationService:` |
| [src/plugins/features/ebm_dataset_builder_v1.py:170](../../src/plugins/features/ebm_dataset_builder_v1.py#L170) | `splits_by_lot =` |
| [src/infrastructure/ingestion/adapters/databricks_client.py:321](../../src/infrastructure/ingestion/adapters/databricks_client.py#L321) | `class DatabricksSQLClient:` |
| [src/infrastructure/ingestion/storage/raw_snapshot_writer.py:66](../../src/infrastructure/ingestion/storage/raw_snapshot_writer.py#L66) | `def directory_fd(` |
| [bootstrap/ingestion_cli.py:128](../../bootstrap/ingestion_cli.py#L128) | `def main(` |
| [bootstrap/ingestion_wiring.py:67](../../bootstrap/ingestion_wiring.py#L67) | `class IngestionSettings:` |
| [bootstrap/ingestion_wiring.py:212](../../bootstrap/ingestion_wiring.py#L212) | `store = LocalRawSnapshotStore(settings.raw_root)` |

## E06

**Governance／deployment／fencing**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [src/application/model_registry/service.py:22](../../src/application/model_registry/service.py#L22) | `def approve(` |
| [src/infrastructure/persistence/ml_training.py:293](../../src/infrastructure/persistence/ml_training.py#L293) | `def finish(` |
| [src/infrastructure/persistence/ml_deployments.py:29](../../src/infrastructure/persistence/ml_deployments.py#L29) | `def create(` |
| [src/application/deployments/service.py:24](../../src/application/deployments/service.py#L24) | `def rollback(` |
| [src/application/deployments/service.py:30](../../src/application/deployments/service.py#L30) | `class DeploymentController:` |
| [bootstrap/ml_cli.py:53](../../bootstrap/ml_cli.py#L53) | `def control(` |
| [src/api/ml_http.py:103](../../src/api/ml_http.py#L103) | `@registry_router.post("/bindings/{binding_id}/approvals", status_code=201)` |
| [src/api/ml_http.py:119](../../src/api/ml_http.py#L119) | `@deployment_router.post("", status_code=202)` |
| [bootstrap/ml_cli.py:75](../../bootstrap/ml_cli.py#L75) | `rows = connection.execute(select(deployments).where(` |
| [src/infrastructure/deployment/local_serving.py:93](../../src/infrastructure/deployment/local_serving.py#L93) | `self._processes[deployment.deployment_id] = subprocess.Popen(` |
| [src/infrastructure/deployment/local_serving.py:110](../../src/infrastructure/deployment/local_serving.py#L110) | `response = httpx.get(metadata["address"]+"/health/ready"` |
| [src/application/deployments/service.py:59](../../src/application/deployments/service.py#L59) | `if not self.executor.ready(deployment):` |
| [src/application/deployments/service.py:67](../../src/application/deployments/service.py#L67) | `self.repository.activate(deployment)` |
| [src/infrastructure/persistence/ml_deployments.py:120](../../src/infrastructure/persistence/ml_deployments.py#L120) | `routes.c.revision == deployment.expected_route_revision).values(` |

## E07

**Gateway／Serving／inference**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [bootstrap/ml_gateway.py:66](../../bootstrap/ml_gateway.py#L66) | `async def predict(` |
| [bootstrap/ml_gateway.py:57](../../bootstrap/ml_gateway.py#L57) | `async def ready(` |
| [bootstrap/ml_serving.py:32](../../bootstrap/ml_serving.py#L32) | `def create_app(` |
| [bootstrap/ml_serving.py:123](../../bootstrap/ml_serving.py#L123) | `def main(` |
| [src/infrastructure/deployment/local_serving.py:62](../../src/infrastructure/deployment/local_serving.py#L62) | `def _launch(` |
| [src/infrastructure/serving/materializer.py:23](../../src/infrastructure/serving/materializer.py#L23) | `class BundleMaterializer:` |
| [src/infrastructure/serving/bounded_prediction.py:21](../../src/infrastructure/serving/bounded_prediction.py#L21) | `class BoundedPredictionExecutor:` |
| [src/infrastructure/serving/bounded_ebm_prediction.py:24](../../src/infrastructure/serving/bounded_ebm_prediction.py#L24) | `class BoundedEBMPredictionExecutor:` |
| [src/application/prediction/service.py:10](../../src/application/prediction/service.py#L10) | `class PredictionService:` |
| [bootstrap/ml_serving.py:50](../../bootstrap/ml_serving.py#L50) | `provider = identity or MongoActorResolver(` |
| [src/infrastructure/serving/materializer.py:41](../../src/infrastructure/serving/materializer.py#L41) | `result = Path(client.download_artifacts` |
| [src/infrastructure/ml_identity.py:44](../../src/infrastructure/ml_identity.py#L44) | `session = self.db.identity_sessions.find_one(` |
| [src/infrastructure/deployment/endpoints.py:26](../../src/infrastructure/deployment/endpoints.py#L26) | `return f"http://{resource_name(deployment)}.` |
| [src/infrastructure/deployment/endpoints.py:36](../../src/infrastructure/deployment/endpoints.py#L36) | `address = json.loads((self.root / deployment.deployment_id / "instance.json")` |
| [src/domain/deployments/identity.py:36](../../src/domain/deployments/identity.py#L36) | `def matches_identity(` |
| [src/infrastructure/persistence/ml_deployments.py:104](../../src/infrastructure/persistence/ml_deployments.py#L104) | `def route(self, tenant: str, endpoint: str) -> Route:` |
| [src/domain/deployments/models.py:7](../../src/domain/deployments/models.py#L7) | `class Route:` |
| [bootstrap/ml_serving.py:111](../../bootstrap/ml_serving.py#L111) | `result = asdict(state["prediction"].predict(` |
| [bootstrap/ml_serving.py:115](../../bootstrap/ml_serving.py#L115) | `result["provenance"].update(spec_digest=trusted_digest` |
| [bootstrap/ml_gateway.py:83](../../bootstrap/ml_gateway.py#L83) | `address = state["resolver"].resolve(deployment)` |
| [bootstrap/ml_gateway.py:94](../../bootstrap/ml_gateway.py#L94) | `if response.is_success and (` |
| [bootstrap/ml_gateway.py:98](../../bootstrap/ml_gateway.py#L98) | `current = await asyncio.to_thread(` |
| [bootstrap/ml_gateway.py:100](../../bootstrap/ml_gateway.py#L100) | `if current != route or response.is_redirect:` |

## E08

**Docker／Compose reference topology**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [compose.ml.yaml:3](../../compose.ml.yaml#L3) | `x-ml-environment:` |
| [compose.ml.yaml:25](../../compose.ml.yaml#L25) | `x-ml-app:` |
| [compose.ml.yaml:35](../../compose.ml.yaml#L35) | `  postgres:` |
| [compose.ml.yaml:38](../../compose.ml.yaml#L38) | `  mlflow:` |
| [compose.ml.yaml:91](../../compose.ml.yaml#L91) | `  migrate:` |
| [compose.ml.yaml:108](../../compose.ml.yaml#L108) | `  supervisor:` |
| [compose.ml.yaml:129](../../compose.ml.yaml#L129) | `  gateway:` |
| [compose.ml.ingestion.yaml:9](../../compose.ml.ingestion.yaml#L9) | `  ingestion-worker:` |
| [compose.ml.databricks.yaml:33](../../compose.ml.databricks.yaml#L33) | `secrets:` |
| [compose.ml.databricks-token.yaml:3](../../compose.ml.databricks-token.yaml#L3) | `services:` |
| [compose.ml.acceptance.yaml:20](../../compose.ml.acceptance.yaml#L20) | `  acceptance:` |
| [compose.yaml:43](../../compose.yaml#L43) | `services:` |
| [Dockerfile:1](../../Dockerfile#L1) | `FROM ` |
| [Dockerfile.ml:1](../../Dockerfile.ml#L1) | `FROM python:3.12-slim AS api` |
| [Dockerfile.ml:27](../../Dockerfile.ml#L27) | `FROM api AS training` |
| [Dockerfile.ml:51](../../Dockerfile.ml#L51) | `FROM ebm AS production` |
| [Dockerfile.mlflow:7](../../Dockerfile.mlflow#L7) | `ENTRYPOINT` |
| [Dockerfile.minio:27](../../Dockerfile.minio#L27) | `FROM runtime AS server` |
| [compose.ml.yaml:79](../../compose.ml.yaml#L79) | `MLFLOW_BACKEND_STORE_URI: postgresql+psycopg://` |
| [compose.ml.yaml:80](../../compose.ml.yaml#L80) | `MLFLOW_S3_ENDPOINT_URL: http://minio:9000` |
| [compose.ml.yaml:131](../../compose.ml.yaml#L131) | `network_mode: service:control` |
| [compose.ml.yaml:29](../../compose.ml.yaml#L29) | `volumes: [./var:/app/var]` |
| [compose.ml.yaml:90](../../compose.ml.yaml#L90) | `minio-init: {condition: service_completed_successfully}` |
| [compose.ml.yaml:127](../../compose.ml.yaml#L127) | `ports: [127.0.0.1:8001:8001]` |
| [compose.ml.yaml:134](../../compose.ml.yaml#L134) | `depends_on: {control: {condition: service_started}}` |

## E09

**Host configuration／startup／platform limits**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [bootstrap/ml_config.py:11](../../bootstrap/ml_config.py#L11) | `class MLSettings:` |
| [bootstrap/ml_config.py:43](../../bootstrap/ml_config.py#L43) | `def from_env(` |
| [bootstrap/ml_cli.py:25](../../bootstrap/ml_cli.py#L25) | `def migrate(` |
| [bootstrap/ml_cli.py:36](../../bootstrap/ml_cli.py#L36) | `async def relay(` |
| [bootstrap/ml_cli.py:53](../../bootstrap/ml_cli.py#L53) | `def control(` |
| [bootstrap/ml_cli.py:100](../../bootstrap/ml_cli.py#L100) | `def demo(` |
| [bootstrap/ml_cli.py:124](../../bootstrap/ml_cli.py#L124) | `def demo_ebm(` |
| [bootstrap/ml_cli.py:159](../../bootstrap/ml_cli.py#L159) | `def main(` |
| [src/infrastructure/execution/local_process.py:19](../../src/infrastructure/execution/local_process.py#L19) | `def process_identity(` |
| [src/infrastructure/execution/local_process.py:63](../../src/infrastructure/execution/local_process.py#L63) | `def inspect(` |
| [src/config/settings.py:10](../../src/config/settings.py#L10) | `class Settings(` |
| [pyproject.toml:21](../../pyproject.toml#L21) | `[project.optional-dependencies]` |
| [worker.py:7](../../worker.py#L7) | `broker =` |
| [server.py:30](../../server.py#L30) | `def create_app(` |
| [README.md:34](../../README.md#L34) | `## Real local training` |
| [docs/configuration-reference.md:11](../../docs/configuration-reference.md#L11) | `## Profile` |
| [bootstrap/ml_cli.py:147](../../bootstrap/ml_cli.py#L147) | `        components.supervisor().execute(command)` |
| [bootstrap/ml_config.py:13](../../bootstrap/ml_config.py#L13) | `database_url: str = field(default="sqlite:///./var/ml.db"` |
| [src/infrastructure/execution/local_process.py:22](../../src/infrastructure/execution/local_process.py#L22) | `tail = Path(f"/proc/{pid}/stat")` |
| [src/infrastructure/deployment/local_serving.py:69](../../src/infrastructure/deployment/local_serving.py#L69) | `reservation.bind(("127.0.0.1", 0))` |

## E10

**External production profile／secrets**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [compose.ml.external-mlflow.yaml:6](../../compose.ml.external-mlflow.yaml#L6) | `x-runtime-environment:` |
| [compose.ml.external-mlflow.yaml:37](../../compose.ml.external-mlflow.yaml#L37) | `x-runtime:` |
| [compose.ml.external-mlflow.yaml:85](../../compose.ml.external-mlflow.yaml#L85) | `services:` |
| [bootstrap/deployment_entrypoint.py:50](../../bootstrap/deployment_entrypoint.py#L50) | `REQUIRED =` |
| [bootstrap/deployment_entrypoint.py:72](../../bootstrap/deployment_entrypoint.py#L72) | `def _read_configuration(` |
| [bootstrap/deployment_entrypoint.py:118](../../bootstrap/deployment_entrypoint.py#L118) | `def load_runtime_environment(` |
| [docs/deployment/day1-deployment-runbook.md:1](../../docs/deployment/day1-deployment-runbook.md#L1) | `# Day-1` |
| [docs/configuration-reference.md:35](../../docs/configuration-reference.md#L35) | ``docker compose exec`` |

## E11

**Implemented Kubernetes profiles／qualification boundaries**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [deploy/k8s/base/workloads.yaml:5](../../deploy/k8s/base/workloads.yaml#L5) | `  name: ml-management` |
| [deploy/k8s/base/workloads.yaml:320](../../deploy/k8s/base/workloads.yaml#L320) | `  name: ml-supervisor` |
| [deploy/k8s/base/workloads.yaml:471](../../deploy/k8s/base/workloads.yaml#L471) | `  name: ml-control` |
| [deploy/k8s/base/workloads.yaml:737](../../deploy/k8s/base/workloads.yaml#L737) | `  name: ml-ingestion-worker` |
| [deploy/k8s/base/services.yaml:17](../../deploy/k8s/base/services.yaml#L17) | `  name: ml-gateway` |
| [deploy/k8s/base/storage.yaml:3](../../deploy/k8s/base/storage.yaml#L3) | `kind: PersistentVolumeClaim` |
| [deploy/k8s/base/networkpolicies.yaml:3](../../deploy/k8s/base/networkpolicies.yaml#L3) | `kind: NetworkPolicy` |
| [deploy/k8s/overlays/minikube-training/training-runtime.yaml:4](../../deploy/k8s/overlays/minikube-training/training-runtime.yaml#L4) | `  name: ml-supervisor` |
| [deploy/k8s/overlays/minikube-serving/control-api.yaml:4](../../deploy/k8s/overlays/minikube-serving/control-api.yaml#L4) | `  name: ml-control` |
| [deploy/k8s/infrastructure/minikube/mlflow.yaml:25](../../deploy/k8s/infrastructure/minikube/mlflow.yaml#L25) | `kind: Deployment` |
| [src/infrastructure/deployment/kubernetes_serving.py:80](../../src/infrastructure/deployment/kubernetes_serving.py#L80) | `class KubernetesServingExecutor` |
| [src/infrastructure/execution/pod_process.py:17](../../src/infrastructure/execution/pod_process.py#L17) | `class PodProcessExecutor` |
| [bootstrap/ml_training_guardian.py:28](../../bootstrap/ml_training_guardian.py#L28) | `def supervise(` |
| [docs/adr/017-kubernetes-runtime-boundaries.md:14](../../docs/adr/017-kubernetes-runtime-boundaries.md#L14) | `## Decision` |
| [docs/adr/018-kubernetes-storage-contracts.md:8](../../docs/adr/018-kubernetes-storage-contracts.md#L8) | `## Decision` |
| [docs/adr/019-kubernetes-serving-runtime.md:7](../../docs/adr/019-kubernetes-serving-runtime.md#L7) | `## Decision` |
| [docs/adr/020-pod-training-runtime.md:7](../../docs/adr/020-pod-training-runtime.md#L7) | `## Decision` |
| [docs/adr/021-kubernetes-acceptance.md:5](../../docs/adr/021-kubernetes-acceptance.md#L5) | `## Decision` |
| [deploy/k8s/base/migration-job.yaml:5](../../deploy/k8s/base/migration-job.yaml#L5) | `  name: ml-migrate` |
| [deploy/k8s/base/services.yaml:12](../../deploy/k8s/base/services.yaml#L12) | `targetPort: 8000` |
| [deploy/k8s/base/services.yaml:20](../../deploy/k8s/base/services.yaml#L20) | `app.kubernetes.io/name: ml-control` |
| [deploy/k8s/base/workloads.yaml:541](../../deploy/k8s/base/workloads.yaml#L541) | `- name: prepare-gateway` |
| [deploy/k8s/base/storage.yaml:8](../../deploy/k8s/base/storage.yaml#L8) | `- ReadWriteOnce` |
| [deploy/k8s/overlays/minikube-training/kustomization.yaml:12](../../deploy/k8s/overlays/minikube-training/kustomization.yaml#L12) | `ML_TRAINING_BACKEND=pod` |
| [deploy/k8s/overlays/minikube-serving/kustomization.yaml:12](../../deploy/k8s/overlays/minikube-serving/kustomization.yaml#L12) | `ML_SERVING_BACKEND=kubernetes` |
| [src/infrastructure/deployment/kubernetes_serving.py:157](../../src/infrastructure/deployment/kubernetes_serving.py#L157) | `"name": "prepare-serving"` |
| [src/infrastructure/deployment/kubernetes_serving.py:243](../../src/infrastructure/deployment/kubernetes_serving.py#L243) | `"immutable": True` |
| [deploy/k8s/infrastructure/minikube/kustomization.yaml:4](../../deploy/k8s/infrastructure/minikube/kustomization.yaml#L4) | `- postgres.yaml` |
| [deploy/k8s/infrastructure/minikube/files/nginx.conf:18](../../deploy/k8s/infrastructure/minikube/files/nginx.conf#L18) | `proxy_pass http://127.0.0.1:5000` |
| [deploy/k8s/infrastructure/minikube/files/01-mlflow.sql:1](../../deploy/k8s/infrastructure/minikube/files/01-mlflow.sql#L1) | `CREATE DATABASE mlflow;` |
| [deploy/k8s/infrastructure/minikube/minio-init-job.yaml:24](../../deploy/k8s/infrastructure/minikube/minio-init-job.yaml#L24) | `- name: initialize` |
| [deploy/k8s/README.md:32](../../deploy/k8s/README.md#L32) | `Single-node engineering` |

## E12

**Verification／observability／static limits**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [scripts/check_boundaries.py:74](../../scripts/check_boundaries.py#L74) | `LAYER_DEPENDENCIES:` |
| [scripts/verify_ml_acceptance.py:50](../../scripts/verify_ml_acceptance.py#L50) | `def main(` |
| [tests/ml/test_training_contracts.py:27](../../tests/ml/test_training_contracts.py#L27) | `def test_concurrent_acceptance` |
| [tests/ml/test_model_governance.py:26](../../tests/ml/test_model_governance.py#L26) | `def test_approval_pins_exact_evidence` |
| [tests/ml/test_pod_training_runtime.py:273](../../tests/ml/test_pod_training_runtime.py#L273) | `async def test_ack_requires_terminal_sql` |
| [.github/workflows/ml-ci.yml:42](../../.github/workflows/ml-ci.yml#L42) | `  contracts:` |
| [bootstrap/ml_telemetry.py:11](../../bootstrap/ml_telemetry.py#L11) | `def configure_telemetry(` |
| [src/infrastructure/ml_tracing.py:1](../../src/infrastructure/ml_tracing.py#L1) | `W3C tracing` |

## E13

**Backend／CPU baseline decisions**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [docs/adr/README.md:44](../../docs/adr/README.md#L44) | `## ADR-008` |
| [docs/adr/README.md:60](../../docs/adr/README.md#L60) | `## ADR-009` |
| [docs/adr/README.md:70](../../docs/adr/README.md#L70) | `## ADR-010` |
| [docs/adr/014-implementation-baseline.md:5](../../docs/adr/014-implementation-baseline.md#L5) | `The upstream source` |
| [docs/adr/014-implementation-baseline.md:7](../../docs/adr/014-implementation-baseline.md#L7) | `1. Freeze` |
| [docs/adr/014-implementation-baseline.md:8](../../docs/adr/014-implementation-baseline.md#L8) | `2. Use` |
| [docs/adr/014-implementation-baseline.md:9](../../docs/adr/014-implementation-baseline.md#L9) | `3. PostgreSQL` |
| [docs/adr/014-implementation-baseline.md:10](../../docs/adr/014-implementation-baseline.md#L10) | `4. Keep` |

## E14

**Approved providers／workloads／initialization**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [docs/adr/016-databricks-batch-ingestion.md:24](../../docs/adr/016-databricks-batch-ingestion.md#L24) | `## Decision` |
| [src/domain/training/workloads.py:61](../../src/domain/training/workloads.py#L61) | `APPROVED_WORKLOADS =` |
| [bootstrap/ml_training_runner.py:96](../../bootstrap/ml_training_runner.py#L96) | `TRAINING_HANDLERS =` |
| [bootstrap/init_cli.py:557](../../bootstrap/init_cli.py#L557) | `def main(` |
| [docs/deployment/interactive-setup.md:1](../../docs/deployment/interactive-setup.md#L1) | `# ` |
| [src/plugins/models/ebm_cnn_v1.py:119](../../src/plugins/models/ebm_cnn_v1.py#L119) | `def build_ebm_cnn(` |

## E15

**Retained evidence／deferred scope**

| Exact source / line | Named symbol or declaration |
| --- | --- |
| [docs/implementation-status.md:6](../../docs/implementation-status.md#L6) | `## 已交付` |
| [docs/implementation-status.md:42](../../docs/implementation-status.md#L42) | `## 尚未完成` |
| [docs/ebm-implementation-status.md:9](../../docs/ebm-implementation-status.md#L9) | `## 已實作` |
| [docs/ebm-implementation-status.md:52](../../docs/ebm-implementation-status.md#L52) | `尚未提供跨 Job` |
| [docs/ebm-integration-plan.md:1](../../docs/ebm-integration-plan.md#L1) | `# ` |
| [docs/evidence/k8s-acceptance-2026-11-18/qualification.json:1](../../docs/evidence/k8s-acceptance-2026-11-18/qualification.json#L1) | `{` |

## 歷史與 PR 證據

以 `git log --first-parent --reverse --format="%h %ad %s" --date=short` 與 `git show --no-patch --format=full <commit>` 取得本機 commit／merge messages。未查遠端 PR 討論；不把 merge title 延伸成未知作者動機。

| Commit | PR / contribution |
| --- | --- |
| [`a88f6ff`](https://github.com/PenHsuanWang/ml-system-prod/commit/a88f6ff) | Initial import；ADR-014 已存在 |
| [`edc1400`](https://github.com/PenHsuanWang/ml-system-prod/commit/edc1400) | EBM training／serving |
| [`122c468`](https://github.com/PenHsuanWang/ml-system-prod/commit/122c468) | PR #1 governed ingress |
| [`72dc1c3`](https://github.com/PenHsuanWang/ml-system-prod/commit/72dc1c3) | PR #2 access token alternative |
| [`97301cf`](https://github.com/PenHsuanWang/ml-system-prod/commit/97301cf) | PR #3 DatasetBuilder |
| [`06ddc3a`](https://github.com/PenHsuanWang/ml-system-prod/commit/06ddc3a) | PR #4 EBM end-to-end |
| [`ce81327`](https://github.com/PenHsuanWang/ml-system-prod/commit/ce81327) | PR #5 approved workload dispatch |
| [`79cc22c`](https://github.com/PenHsuanWang/ml-system-prod/commit/79cc22c) | Immutable workload registry refactor |
| [`f2040ed`](https://github.com/PenHsuanWang/ml-system-prod/commit/f2040ed) | PR #7 K8s foundation |
| [`3150c04`](https://github.com/PenHsuanWang/ml-system-prod/commit/3150c04) | PR #8 storage |
| [`a5c3847`](https://github.com/PenHsuanWang/ml-system-prod/commit/a5c3847) | PR #9 native Serving |
| [`336615e`](https://github.com/PenHsuanWang/ml-system-prod/commit/336615e) | PR #10 Pod training |
| [`55dd226`](https://github.com/PenHsuanWang/ml-system-prod/commit/55dd226) | PR #11 governed acceptance |
| [`6b536ea`](https://github.com/PenHsuanWang/ml-system-prod/commit/6b536ea) | PR #12 documentation |
| [`8c9023b`](https://github.com/PenHsuanWang/ml-system-prod/commit/8c9023b) | PR #13 initialization |
