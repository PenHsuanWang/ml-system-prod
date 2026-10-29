# Backend 驗收報告 — 2026-10-29

**結論：本機功能與回歸檢查通過；完整 Backend／Production 驗收未通過。** 真實服務與指定 Runtime 的必要證據不足，不能以測試跳過或 CLI demo 代替。

本次重新執行測試、補上 Backend HTTP 生命週期與故障注入測試，並修正可重現的缺陷。原始紀錄與機器可讀結果見 [驗收證據](evidence/backend-acceptance-2026-10-29/README.md)。

## 已執行結果

| 項目 | 結果 | 證據範圍 |
| --- | --- | --- |
| ML／架構測試 | 217 passed、3 skipped、0 failed | SQLite、真實 CPU PyTorch 子程序；HTTP 使用 ASGI transport |
| Backend 生命週期 | 通過 | API 提交／並行冪等／訓練／查詢／人工核准／啟用路由／推論／重建 Repository／提出新 generation rollback |
| 品質拒絕 | 通過 | 真實訓練後以固定 held-out labels 觸發 REJECTED，人工核准被拒 |
| 評估執行錯誤與復原 | 通過 | 持久化 ERROR、安全錯誤碼、拒絕核准、沿原 execution 復原成功且不重新 fit |
| 故障／競爭 | 通過 | 接受交易 rollback、100 次晚到結果、Serving 啟動競爭、ready=false、啟動中斷、推論容量 |
| 架構、核心型別、lint、compile | 通過 | import boundaries、30 個核心檔案 strict mypy、新增 ML 程式 unused-import 檢查、source compile |
| Compose 設定 | 通過 | 實際 Docker Compose v2.27.1 驗證主設定與驗收 override；未啟動容器 |
| 完整模板測試收集 | 環境阻擋 | Python 3.10 不提供 UTC／Self／StrEnum，且缺少必要套件；16 個 collection errors，不能宣稱通過 |
| 真實角色與服務整合 | 未執行 | 2 個 Receiver 案例及 1 個完整 HTTP stack 案例跳過 |

ASGI 測試明確使用 Identity 與 Launcher 替身；運算與 SQL 不是替身。這些結果不證明 JWT／Mongo、RabbitMQ、MLflow／MinIO、實際網路監聽與容器部署已通過。

## 發現並修正

| 缺陷 | 修正 | 回歸證據 |
| --- | --- | --- |
| 兩個 Controller 可同時啟動同一 Serving generation | 在共用 execution store 原子建立排他 launch intent；中斷後不盲目再啟動 | `test_concurrent_serving_prepare_creates_only_one_process` |
| readiness 只核對身份，忽略 `ready=false` | 必須同時明確回報 `ready=true` | `test_serving_requires_positive_readiness` |
| port 配置失敗直接拋出 OSError，無停止證據 | 正規化為 launch failure；只在確定未產生 child 時留下可釋放證據 | `test_failed_port_allocation_is_a_safe_closed_launch` |
| launch intent 存在但 instance 未寫入時永久等待 | 依 launch intent 時間進入需對帳的失敗處理；不假稱已停止 | `test_incomplete_serving_launch_times_out_without_claiming_stop` |
| Dataset 讀取失敗時 Evaluation Runtime 未關閉 | 載入資料與推論共用 try/finally cleanup | `test_dataset_failure_closes_the_evaluation_runtime` |
| 評估異常沒有持久 ERROR 報告 | 記錄不可變 ERROR 與安全錯誤碼，保留有界復原並阻擋核准 | `test_evaluation_error_is_durable_and_recovery_does_not_refit` |

前五項先以新測試重現為 **5 failed / 5 passed**，修正後回歸通過；原始失敗報告保存在驗收證據目錄。另加防禦性處理：不完整 process identity 不作停止證據，已退出／zombie process 不誤發訊號。

## LLD 關鍵案例對照

| LLD ID | 本次判定 | 限制 |
| --- | --- | --- |
| ML-T01／02 | 本機通過 | 並行提交、相同 key 重送與不同 spec 衝突 |
| ML-T03／04 | 部分通過 | SQL rollback、Owner 與不重新 fit 已驗證；真實 PostgreSQL／Broker 待執行 |
| ML-T05／06 | 部分通過 | 終態與重送 guard 已驗證；真 Receiver ACK barrier／lost ACK 未執行 |
| ML-T07／08 | 本機通過 | 真實 CPU 子程序取消與 checkpoint/resume |
| ML-T09／10 | 本機通過 | durable Evaluation ERROR／復原與真實品質拒絕，不可核准 |
| ML-T11 | 未驗證 | MLflow 註冊 response lost／canonical reconciliation 需真服務故障注入 |
| ML-T12／13／14 | 部分通過 | 本機 Artifact、Golden、schema 檢查；乾淨目標 Image 待驗證 |
| ML-T15／16／17 | 部分通過 | readiness、SQL fencing、回滾 generation 已驗證；多角色流量切換待驗證 |
| ML-T18 | 部分通過 | Actor／Tenant 矩陣通過；真實 JWT session／Mongo 待驗證 |
| ML-T19 | 部分通過 | 已載入模型可在 artifact source 不可用時推論；真 MLflow outage 待驗證 |
| ML-T20 | 本機通過 | 100 次失敗後容量可重用；逾時不提前釋放實體 slot |
| ML-T21／22 | 未驗證 | 指定 Runtime qualification、完整備份還原與對帳 |

## 環境阻擋與重試

收到再次嘗試指示後，重新執行 Docker、Compose、loopback 與 DNS 檢查：

- Docker Client 存在，但 `/var/run/docker.sock` 回覆 permission denied。
- 獨立 `docker-compose` 可用，設定驗證成功；`docker compose` plugin 未安裝。
- `127.0.0.1` bind 回覆 `EPERM`，無法啟動實際 HTTP Listener。
- 套件站台 DNS 解析失敗，無法安裝缺少的 Runtime／SDK。
- 只有 Python 3.10.12／cached Torch 2.0.1；目標 Python 3.12／Torch 2.6.0 未驗證。

主機的 asyncio selector workaround 僅用於測試，不修改 application event loop。限制解除前，完整驗收維持未通過。

## 可重跑的驗收入口

安裝受支援環境依賴後執行本機檢查：

```sh
python scripts/verify_ml_acceptance.py --output-dir var/acceptance/local
```

完整服務驗收由 [acceptance Compose override](../compose.ml.acceptance.yaml)、[acceptance Docker target](../Dockerfile.ml) 與 [CI backend-stack job](../.github/workflows/ml-ci.yml) 提供。使用獨立 Compose project、私有測試環境設定與 `ML_ACCEPTANCE_VAR_DIR`，先啟動 management／relay／supervisor／control／gateway，再執行一次性 acceptance service。不要用 production DB／Identity／Artifact Store 跑測試。

```sh
export ML_ACCEPTANCE_VAR_DIR="$PWD/var/acceptance-stack-data"
mkdir -p "$ML_ACCEPTANCE_VAR_DIR"
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ml-acceptance up --build -d management relay supervisor control gateway
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ml-acceptance run --build --rm --no-deps acceptance
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ml-acceptance down --volumes --remove-orphans
```

`.env.ml` 必須先按 README 建立；現有檔案不會被產生器覆寫。`--require-stack` 缺少必要端點時回傳非零；指定 Runtime 不符、必要案例跳過或任一檢查失敗也不能得到完整功能驗收通過。CI workflow 尚未遠端執行；Production 還需要不可變 image／lock、安全、負載與 restore 證據及產品核准。
