# v1.3 工程交付驗證證據

彙整日期：2026-10-26（Asia/Taipei）。原始檔保留實際執行時間；Python 3.11 前置驗證於 2026-10-24 執行，其後生產 Python 邏輯未變，最後 Docker／Python 3.13／套件與安全驗證於 2026-10-26 完成。文件及 CI 的最後調整另經靜態檢查。

- [正式映像與真服務整合](docker/report.json)：非 root、原始 CMD、無 pip／ensurepip、雙 API／雙 Taskiq、32 筆工作、重啟與登出；[host pytest](docker/pytest.log) 為 444 passed。此 pytest 在主機 Python 執行，外部服務為真容器。
- [Python 3.13 單元結果](delivery-final-unit.log) 與 [coverage](delivery-final-coverage.json)：440 passed、4 integration deselected，整體 86.47%；[Linux Python 3.11](delivery-python311.log) 整體 86.60%。兩者啟用 branch 量測，百分比為 coverage.py 合併 line／branch 指標。
- [Python 3.13 套件驗證](package/report.json) 與 [Python 3.11](package-python311/report.json)：新 venv 真正非 editable 安裝、專案外 isolated import、封鎖外部連線、disabled lifespan／health／cleanup；package/ 同時保存受驗 wheel／sdist 與日誌。
- [手冊同步／背景重跑](guide/summary.json)：10 類檢查皆通過；[code fences 一致性](guide/fence-consistency.json) 記錄目前及舊驗證版本的關係。
- [全新能力容器驗證](adoption/report.json)：新副本明確 wiring 的 items 讀寫與背景工作、真 Mongo、tenant／owner 授權及 job 結果隔離；[驗證腳本原文](adoption/run.py.txt) 保存當時副本路徑及命令，可配合 scripts/verify_guide.py 重建。其餘平台 Python 檔案維持相同，能力檔案與 wiring 為允許擴充點。
- [依賴稽核](delivery-release-dependency-audit.json)：111 筆 dev／build 條目（可能有重複），無已知漏洞；[Bandit](delivery-release-bandit.json) 通過 medium severity／confidence 門檻。
- [Trivy gate](delivery-release-trivy-summary.json)、[完整掃描](delivery-release-container-vulnerabilities.json) 與 [CycloneDX SBOM](delivery-release-container-sbom.cdx.json)。有修正的 HIGH／CRITICAL 為零；基底未修復項目完整保留。先前 pip／ensurepip 的內附套件發現已藉實際移除解決，沒有加入 Trivy 忽略規則。
- [映像建置來源](image-source-manifest.json) 綁定 Docker COPY 使用的來源；最終 source-manifest.json 綁定整體交付內容（排除自身及 SHA256SUMS，避免自我雜湊）。

Secret 掃描已通過 Git 歷史與交付工作樹兩個範圍，結果存於 checks/secret-scan-summary.json；workflow／靜態結果在 checks/summary.json，文件／JSON(L)／SVG／wheel 與新副本來源核對在 document-checks.json。最後來源 manifest 與 SHA256SUMS 隨本目錄保存。所有成功 run 的臨時 secrets.env 已刪除，容器／network／volumes 已清理；Docker image 保留。CI 上傳規則另外排除清理失敗時可能保留的 secrets.env。
