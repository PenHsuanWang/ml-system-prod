# Docker／RabbitMQ 最終通過證據

日期：2026-10-24（Asia/Taipei）。最終 run：`backend-verify-605e70e128ef`。`report.json` 的 `passed` 與 `cleanup_complete` 均為 true。

| 證據 | 用途 |
| --- | --- |
| [report.json](report.json) | 最終九類檢查、API/worker PIDs、queue 狀態、版本、image ID |
| [pytest.log](pytest.log) | 完整 pytest：406 passed，10.68 秒，0 skipped |
| [image-runtime.json](image-runtime.json) | 實際 image Python／UID／套件版本 |
| [image-source-manifest.json](image-source-manifest.json) | 映像來源檔案 SHA-256 |
| [build.log](build.log) | 最終 cached build 與 image tag |
| [build-uncached.log](build-uncached.log) | 先前完整建置；其 image ID 與最終測試完全相同 |
| [default.log](default.log) | 原始 CMD 的 readiness、乾淨 OpenAPI、正常關閉 |
| [startup.log](startup.log)、[containers.json](containers.json) | 隔離 stack 啟動與健康狀態 |
| [stack.log](stack.log) | 同一 image 內 API／worker 與三項基礎服務日誌 |
| [cleanup.log](cleanup.log) | 僅本次 project 的 containers/network/volumes 清除 |

所有複製的程序輸出均經 runner 對本次隨機密碼、JWT key 與已知 access token 做遮蔽；沒有複製 secrets.env 或 pytest 暫存資料庫。測試資料及憑證均為此隔離 stack 臨時生成。完成後另以 project label 查詢 containers、volumes、network，結果皆空；Docker image 保留。

`SHA256SUMS` 涵蓋機器報告與原始證據檔（不含本導讀）；各檔為本次測試紀錄，不是效能或 HA 認證。完整操作與界線見 [操作手冊](../../docker-validation.md) 及 [驗證總表](../../validation.md)。
