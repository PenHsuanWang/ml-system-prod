# ML Runtime Compatibility Matrix

| 項目 | 內容 |
| --- | --- |
| 版本 | ML-COMPAT-001／0.2，2026-10-27 |
| Owner | DS＋Backend＋Platform；各 qualified row 有明確核准者 |
| 對應設計 | [ML-SAD-001 §16.5](ml-system-design.md#165-依賴與映像) |
| 現況 | 尚無合格 ML runtime；下表不宣稱 MLflow／sklearn 已安裝或驗證 |

## 1. 支援狀態

`proposed` 表示待選版，`testing` 表示精確組合已建置待驗，`qualified` 表示指定來源／image／模型 fixtures 的必要測試通過，`retired` 表示停止新部署。只有 qualified 且符合目標環境政策的 row 可進入正式 ModelLoader。

平台 Python／Taskiq 的單元測試，不能替代模型反序列化與推論的相容性證據。MLflow client 與 server 分別記版本；更換任一 serializer、依賴、OS／arch 或 image digest 都建立新 row 或重新驗證，不只比較 major version。

## 2. 現有紀錄

| Compatibility ID | Python | MLflow client／server | sklearn／NumPy | Serializer／policy | OS／arch／image | 狀態 |
| --- | --- | --- | --- | --- | --- | --- |
| `platform-baseline-08490e84` | 3.13.11 | 未驗證 | 未驗證 | 不適用 | macOS arm64；host venv、非 ML image | platform-only，不供模型載入 |
| `cpu-sklearn-v1` | 待鎖定 | 待鎖定 | 待鎖定 | 待選格式、版本與 trusted-type policy | 待選 Linux／arch／OCI digest | proposed，禁止宣稱可部署 |
| `example-not-qualified` | fixture | fixture | fixture | fixture | 合成 image digest | 永遠不列入 qualified allowlist |

第一列的實際平台檢查見 [符合性審查](ml-system-compliance-review.md)；沒有從該結果推導未知 ML 套件版本。第二列是產品實作前的選版工作，不是支援承諾。

## 3. 每份 qualified record 的完整欄位

| 欄位 | 要求 |
| --- | --- |
| Identity | compatibility ID、record version／digest、owner、批准日期、狀態 |
| Environment | Python 完整版本、OS／arch、libc、必要 native library；GPU 模式另列 CUDA／driver／framework |
| Tracking | MLflow client／server 各自精確版本及 API／flavor 參數 |
| Model | sklearn、NumPy／SciPy 與必要 framework 的精確版本 |
| Serialization | 格式、serializer package／版本、trusted types／code-loading policy digest |
| Artifact | 受驗模型／pipeline／signature／manifest digests、golden inputs／outputs |
| Runtime | Training／Serving image ref＋digest、程式 commit、hash lock／SBOM refs |
| Evidence | load、predict、schema reject、close／cancel、cold start、rolling／rollback、資源量測報告 |
| Lifecycle | 可接受來源版本、migration／retrain 要求、retirement／rollback 保留期限 |

不同 framework 不需使用同一 serializer，但必須符合對應安全與可重現性契約。PyFunc、sklearn 原生 load、ONNX／remote runtime 為不同 adapter profile，不能彼此挪用資格。

## 4. Qualification Gate

1. 以 locks 與固定 base／runtime image 建置，取得可核對來源與 SBOM。
2. 真正序列化代表性模型與 preprocessing，從乾淨 Serving image 載入。
3. 執行所有適用的 ModelLoader／Runtime 共同行為、golden、拒絕錯 schema／格式、cleanup 及 rollback 測試。
4. 保存實際 resource／latency 與失敗報告；聲明 thread safety／batch／hard-cancel capability。
5. Owner 核對 image／record／artifact digests 後改 qualified；Serving 與 Controller 驗相同 allowlist。

ML-T37 檢查空欄、未驗組合與偽造資格；M3 同時要求數值化 [Acceptance Profile](ml-system-design.md#174-m3-的可量測目標閘門)。目前沒有執行這些 ML runtime tests，也沒有設定生產閾值。
