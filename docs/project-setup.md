# 本機專案建立紀錄

建立日期：2026-10-24（Asia/Taipei）。工作目錄：`/Users/pwang/Developer/python-backend-server-template`。

本專案由已驗證的 prototype 複製而來，160 個原始碼、測試、設定範本與設計／驗證文件逐檔 SHA-256 一致。原始交付目錄與 ZIP 保留。新增的本機 Git 與 `.venv` 分別在此目錄建立，沒有複製舊虛擬環境、快取、臨時帳密或執行中的資料庫。

移轉當時本機 Git 初始化於 `main`；其後已建立 commit 並設定 GitHub remote。以下測試數與環境屬移轉當時紀錄；目前交付狀態以 [handover](handover.md) 與 [validation](validation.md) 為準。Python distribution 名稱維持 `modular-fastapi-backend`，不依賴資料夾名稱。

## 開始開發

```bash
cd /Users/pwang/Developer/python-backend-server-template
source .venv/bin/activate
python server.py --reload
```

此 `.venv` 使用 Python 3.13.11，已依 `requirements.lock` 安裝 editable `.[dev]`；60 個鎖定套件版本核對一致。預設不需要 `.env` 即可啟動空白平台，健康端點與 API 文件可用。選用身份／資料庫與 Docker 開發環境設定見 [README](../README.md)。本次只保留 `.env.example`，沒有生成本地 `.env` 或正式帳號。

若重新建立環境：

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-build.lock
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
```

獨立啟動 Docker 開發 stack 時，完成 README 的憑證設定後，可用 `docker compose --project-name python-backend-server-template up --build -d` 明確指定此專案名稱；後續 logs／down 使用同一個 `--project-name`。Compose 仍使用 README 列出的本機連接埠。

## 本次轉移驗證

- 160 個移入檔案與保留來源逐檔一致。
- 新 `.venv` 執行 pytest：405 passed，1 skipped，14.38 秒。略過項為需明確啟用真實 RabbitMQ／Redis 的多程序測試；先前完整 Docker run 為 406 passed、0 skipped，見 [已保留證據](evidence/docker-2026-10-24/README.md)。本次轉移沒有再啟動該 stack。
- Ruff、格式、Mypy（102 source files）、依賴邊界與 pip check 通過。
- 從專案外 CWD 以 Python isolated mode 載入 editable 安裝，server／worker／manage／domain strategy 全來自本目錄。
- 真實 HTTP readiness／liveness 200、預設 OpenAPI 僅兩個健康端點、request ID 回傳正常。SIGTERM 後完成 lifespan shutdown，測試服務已停止。Uvicorn 在完成 shutdown 後重送收到的 SIGTERM，因此 subprocess return code 為 -15。

完整移轉 manifest、測試與 HTTP 日誌保存在原交付目錄的 `outputs/project-transfer/`。模組功能規範見 [module-spec](module-spec.md)，架構規格見 [software-design](software-design.md)。
