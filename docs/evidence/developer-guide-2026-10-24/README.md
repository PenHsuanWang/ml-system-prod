# Developer Guide 交付審核證據

日期：2026-10-24（Asia/Taipei）。來源為本機 v1.2 工作樹與 developer-guide 文件；本次沒有在正式專案註冊教學能力。

- [整體命令與結果](summary.json)：同步、背景兩個副本的 boundary、Ruff、format、strict Mypy、文件測試與額外回歸；12 類檢查 exit code 皆 0。
- [選用 kernel 契約](kernel-contract.json)：binding、直接執行、輸入錯誤與通用 service 例外包裝，4 項檢查通過。
- [審查測試原文](test_items_contract.py.txt)／[背景測試原文](test_items_background_contract.py.txt)：以 `.txt` 保存供審查，沒有加入正式 pytest 收集路徑。
- [Compose 設定](compose-summary.json)：兩個程序的能力開關確認，沒有啟動容器，dummy env 已刪除。
- [交付文件雜湊與 code-fence 一致性](delivered-document.json)：完整文件因追加審核文字而改變 SHA，受驗證的 Python／YAML 區塊逐一相同。
- [同步提取 mapping](final-sync-mapping.json)／[背景提取 mapping](final-background-mapping.json)：說明每個 Python 區塊的檔案落點及完整替換／追加。

同步結果為文件 4 passed＋額外 14 passed；背景結果為文件 4 passed＋額外 25 passed。範例間包含共用案例，不應將加總視為獨立平台測試數。各項 `*.log` 保留實際輸出。驗證時使用本機 Python 3.13.11 與鎖定依賴。

這些檢查使用 fake ports／collection／identity 與組裝替身。沒有聲稱新應用已通過真 MongoDB／RabbitMQ Docker 端到端、完整 JWT 流程、性能或生產部署驗收。原始草稿的 owner 與重送問題已在最終範例修正；本目錄僅保存最終通過紀錄。
