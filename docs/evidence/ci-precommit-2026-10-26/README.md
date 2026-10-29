# CI／pre-commit 增補驗證證據

日期：2026-10-26（Asia/Taipei）。目標專案：`/Users/pwang/Developer/python-backend-server-template`。
基底：`0ac0bda3035d17e90cd01670a3045a4271ce871c`；驗證對象為該基底之上的本機工作樹。

- `commands.json` 記錄已成功執行的指令及範圍，`summary.json` 彙整結果。
- `hooks.log` 含追蹤檔全檢查與新增設定／操作文件檢查。
- `hook-and-gate-cases.json` 保存 13 項結果；含拒絕不合法輸入與 gate 失敗／取消／跳過，預期拒絕以 exit 1 表示。
- `python313-unit.log`／`python313-coverage.xml` 與 `linux-python311.log` 為本輪單元及 coverage。3.11 在固定 digest 的乾淨 Linux 容器 venv 執行；3.13 使用現有 macOS 專案 venv 以 hash lock 更新。
- `guide.log`／`package.log` 保存原有交付驗證器的本次結果；未把範例註冊至正式 wiring。
- `dependency-audit.json`／`dependency-audit.log` 是 dev／build 的 strict hash 稽核，120 筆條目包含重複。
- `gitleaks.json`／`gitleaks.log` 是增補來源快照的 directory 掃描；不將此結果描述成 Git 歷史掃描。
- `final-pending-files-hooks.log` 與 `document-checks.json` 補驗包含新增檔案的來源清單、Markdown 連結／fences 與新 JSON；不同時間的 file count 只描述該次檢查快照。
- `source-manifest.json` 記錄本次工程設定、locks 與正式操作文件的 SHA-256；`SHA256SUMS` 覆蓋本目錄所有證據檔（不含該清單本身）。

實際 Git hook 於隔離 Git 副本執行，該副本使用專案已安裝環境及 staged 來源，沒有建立 commit。合成錯誤輸入只短暫存在於 ignored 的 work 目錄，已刪除；沒有保存私鑰或本機 env fixture。原專案僅安裝 hook，未修改使用者 staging、commit 或遠端。

新增設定與團隊操作見 [CI／pre-commit 規範](../../ci-precommit.md)。本次不重建 image、不重跑 RabbitMQ／Redis／MongoDB 整合；舊 Docker 證據保留原樣。遠端 Actions、branch protection 及 merge queue 不在此本機驗證範圍；現行 workflow 未啟用 merge_group 事件。
