# Docker／RabbitMQ 整合驗證操作手冊

本手冊提供可重現的驗證程序與結果判讀方式。本次實跑結果由 `docs/validation.md` 與 evidence JSON 補列；實際完成項目、執行時間、映像 ID、套件版本及限制，以[驗證紀錄](validation.md)與該次 `report.json` 為準。

## 1. 驗證入口與執行條件

從專案根目錄執行 [scripts/verify_docker.py](../scripts/verify_docker.py)。需要可連線的 Docker Engine、支援 `up --wait` 的 Docker Compose，以及建置／下載所需映像與 Python 依賴的連線能力。執行前可確認目前 Docker context 與服務狀態：

```bash
docker context show
docker version
docker compose version
```

Runner 本身使用 Python 標準函式庫；專案支援 Python 3.11 以上。完整驗證需在已安裝開發依賴的 Python 環境執行，因為 `--host-tests` 會使用同一個 Python 執行 pytest：

```bash
python -m pip install -c requirements.lock -e '.[dev]'
python scripts/verify_docker.py --host-tests
```

只執行容器與 HTTP 整合驗證、略過額外的 host pytest，可使用：

```bash
python scripts/verify_docker.py
```

可指定證據輸出根目錄：

```bash
python scripts/verify_docker.py --host-tests --output-dir work/docker-validation
```

每次執行都建立新的 run ID；相同輸出根目錄下仍會使用不同子目錄與不同 Compose project。首次執行包含 image build 與基礎服務 image pull，所需時間依網路、CPU 及 Docker 可用資源而異。

## 2. 正式映像與測試 fixture 的分離

Runner 使用正式 [Dockerfile](../Dockerfile) 建置 `local/backend-validation:<run-id>`，先以原始 CMD 啟動獨立 smoke container。此階段不掛載測試程式、不連接外部基礎服務，驗證預設空白平台可以獨立啟動。

後續使用同一 image 啟動 [compose.validation.yaml](../compose.validation.yaml) 定義的獨立 stack。API 與 worker 將專案的 `tests/` 唯讀掛載至 `/app/tests`，入口為 [tests/fixtures/docker_runtime.py](../tests/fixtures/docker_runtime.py)：

- API：`tests.fixtures.docker_runtime:create_app`，兩個 Uvicorn worker processes。
- Task worker：`tests.fixtures.docker_runtime:broker`，兩個 Taskiq worker processes，每個最多同時執行兩項 async 任務。
- Fixture 保留正式 `build_modules(settings)` 的 IAM 初始化，額外註冊 `probe.echo` 測試 kernel；版本為字串 `"1"`，resource group 為 `compute`。
- `probe.echo` 短暫 async 等待後回傳輸入 value 與處理程序 PID，僅用來證明任務確實跨 RabbitMQ 進入 worker。正式 bootstrap 不會註冊此 fixture。

驗證 stack 使用 RabbitMQ 4 quorum queue、Redis 7、具帳密驗證的 MongoDB 8，並啟用 JWT IAM、kernel API 及 Redis 資源協調。`compute` 共享容量為 2；資料匯出 API 關閉。此 stack 是測試部署，與正式 [compose.yaml](../compose.yaml) 分開管理。

## 3. 九類檢查及其證據

下表說明 Runner 的檢查契約；出現在表格中不代表該次已通過。成功完成的項目才會寫入 `report.json` 的 `checks`。

| 類別 | 執行內容 | 可觀察證據 |
| --- | --- | --- |
| 1. 正式 image 建置 | 以正式 Dockerfile 建置，檢查 image 設定的執行使用者 | Image ID、OS／architecture、建置日誌、非 root 設定 |
| 2. 原始 CMD 與 lifespan | 原始 CMD 啟動，確認 container 內 UID、readiness、預設 OpenAPI、測試 fixture 未包入 image，並正常停止 | `default.log`；OpenAPI 僅有 `/health/live`、`/health/ready`；application shutdown complete |
| 3. RabbitMQ 拓撲與 consumer | 等待實際 broker queue 建立，確認 quorum 類型及兩個 consumer | Queue name、type、consumers |
| 4. Mongo／IAM／JWT 權限 | 經管理 CLI 建立 admin，測試登入、`me`、受控註冊、匿名拒絕及一般帳號的 kernel 執行拒絕 | 登入／註冊成功；缺 token 為 401；缺執行權限為 403 |
| 5. 跨程序真任務 | 以 admin token 提交 32 項背景 kernel 任務，從共享 job API 讀取結果 | 32 項結果正確；兩個 Taskiq PID 實際執行；兩個 API PID 實際讀取 job snapshots |
| 6. Broker 確認與排空 | 等待測試 queue 不再含待處理及未確認 delivery | `messages_ready=0`、`messages_unacknowledged=0` |
| 7. API 重啟後的共享狀態 | 重啟 API container，再以既有 token 讀取既有 job | Readiness 恢復；Mongo session 與 Redis snapshot 仍可用 |
| 8. Host 完整測試，可選 | 使用 `--host-tests` 時，將本次容器的動態埠交給完整 pytest，啟用真實 RabbitMQ／Redis 多程序及 Mongo 整合測試 | `pytest.log`；該次測試數量、通過／略過／失敗結果 |
| 9. 登出撤銷 | 登出本次 admin session，再使用同一 token 呼叫 `me` | 登出 204，後續使用該 token 為 401 |

API container 重啟測試涵蓋 API process／container 的恢復及共享狀態讀取；資料庫與 broker 在此步驟保持運作。Queue 排空是本次 workload 的觀察結果，不代表任意故障時都不會重複執行。

## 4. 隔離、暫存憑證與日誌

每次執行使用 `backend-verify-<run-id>` 作為 Compose project，並使用獨立 queue namespace、network 與 named volumes。Default smoke container 也使用該次唯一名稱。API、RabbitMQ、Redis、MongoDB 的 host ports 都綁定 `127.0.0.1` 並動態分配；RabbitMQ management port 不對 host 發佈。

Runner 為 RabbitMQ、MongoDB、JWT 及 admin password 產生一次性隨機值；Compose 所需設定寫入該次輸出目錄的 `secrets.env`，權限為 `0600`。管理員密碼經 stdin 傳給 `manage.py`，不放在 CLI 引數中。正式專案 `.env` 不需要加入這些測試憑證。

儲存的 command logs 與 `report.json` 會遮蔽本次產生的 secret，以及登入後取得的 access tokens。此遮蔽僅涵蓋 Runner 已知的值，並非任意敏感資料掃描器；`secrets.env` 本身是私密操作檔，不能作為公開交付證據。

## 5. 結果判讀與清除

預設證據目錄為：

```text
work/docker-validation/backend-verify-<run-id>/
```

依執行進度可能包含 `build.log`、`default.log`、`startup.log`、`containers.jsonl`、`stack.log`、`cleanup.log`、`pytest.log`、pytest 暫存目錄及 `report.json`。尚未執行至對應階段時，該階段日誌可能不存在。

Runner 正常返回時，成功的 exit code 為 0；驗證失敗或資源清除失敗則返回 1。應同時確認 `report.json` 的 `passed: true`、`cleanup_complete: true`，並依是否指定 `--host-tests` 確認完整測試檢查項目。若某項失敗，先檢查 `error`、`cleanup_error`、已完成的 `checks` 與對應日誌。

Runner 在結束階段收集日誌，移除本次 smoke container，並針對本次 project 執行 Compose down 及移除該 project volumes。日誌收集失敗仍會繼續嘗試清除資源。Docker preflight 失敗、尚未建立任何容器時，不會執行 stack down。建置 image 與 Docker build cache 保留，便於後續檢查。

若資源清除成功，Runner 刪除 `secrets.env`。若清除失敗，則保留 `0600` 憑證檔，並在 `report.json` 的 `credentials_retained_for_cleanup` 記錄位置，供恢復 Docker Engine 連線後定向清除。此時應從該份 report 取得**同一次執行**的 project 名稱及憑證檔路徑，再從專案根目錄執行：

```bash
docker compose --env-file <該次secrets.env的路徑> \
  --project-name <該次report.json的project值> \
  -f compose.validation.yaml down --volumes --remove-orphans --timeout 15
```

若 default smoke container 尚未移除，其名稱為 `<該次project值>-default`，可僅針對該名稱執行 `docker rm --force`。確認該次資源已清除後，再刪除保留的 `secrets.env`。上述操作以唯一 project／container 名稱限定範圍，不需要清除其他專案或執行 Docker 全域 prune。

## 6. 驗證範圍與後續部署驗收

此程序驗證單一 Docker Engine 上的映像、程序分工、真實 RabbitMQ 任務路徑、共享狀態及基本認證授權。它不驗證跨主機 HA、RabbitMQ／Redis／MongoDB 叢集故障切換、備份還原、DR、網路分割或資料持久化服務的重啟恢復。

本架構不承諾 exactly-once 執行；單次成功、queue 排空與 terminal snapshot 防重複處理，不能取代外部副作用的冪等設計。32 項短時間測試任務也不能作為吞吐量、延遲 SLO、長時間穩定性或生產容量的證明。產品導入仍需依實際拓撲、資料與 workload 另行執行效能、故障恢復及部署驗收。

## 2026-10-24 最終實跑

以下為 2026-10-24 的歷史 run；最新 v1.3 結果見 [驗證紀錄](validation.md)。當時已依上述流程完整通過；[九類檢查與 image ID](evidence/docker-2026-10-24/report.json)、[406 passed／0 skipped](evidence/docker-2026-10-24/pytest.log) 與 [證據導讀](evidence/docker-2026-10-24/README.md) 已保存。動態 host port 在 container restart 後可能重新分配，runner 會重新查詢後再驗證狀態。先前兩次檢查腳本問題修正後，才以最後一次完整重跑列為通過。
