# 文件導讀

本目錄以目前已合併的 CPU MLP／EBM 與 Kubernetes 工程基線為準。
先讀 [實作與驗收狀態](implementation-status.md)，再依用途選擇文件。
工程驗收通過不代表企業資料、模型品質或正式環境已核准。

## 日常入口

| 用途 | 文件 |
| --- | --- |
| 專案概覽、安裝與 API 範例 | [專案 README](../README.md) |
| 現行架構、角色與資料所有權 | [架構導讀](architecture.md) |
| 已完成能力、證據與剩餘工作 | [實作狀態](implementation-status.md) |
| Runtime、映像與相容性邊界 | [Runtime 相容性](ml-runtime-compatibility.md) |
| 實際環境鍵、預設值、角色與 timeout 限制 | [設定參考](configuration-reference.md) |
| API 權限、冪等、取消與錯誤 | [ML 操作契約](ml-runbook.md#api-與-client-契約) |
| 新增受核准 ML workload | [Workload Extension Contract v1](workload-extension-contract.md) |
| EBM DatasetBuilder、訓練與品質政策 | [EBM 使用說明](ebm-implementation-status.md) |
| File／Databricks 取得原始快照 | [Data Ingress](data-ingress-implementation.md)、[Databricks 手冊](databricks-ingestion-runbook.md) |
| 合成資料完整 EBM 驗收 | [EBM E2E](ebm-e2e-verification.md) |

## 部署、回歸與維運

| 場景 | 操作入口 |
| --- | --- |
| K8s overlay 選擇與適用範圍 | [部署宣告索引](../deploy/k8s/README.md) |
| 外部基礎服務、單機／VM 部署 | [Day-1 與 Go／No-Go](deployment/day1-deployment-runbook.md) |
| Ownership、故障對帳、完整 Compose 回歸、備份還原 | [ML 維運手冊](ml-runbook.md) |
| K8s 完整治理生命週期與故障測試 | [K8s Acceptance](deployment/kubernetes-acceptance.md) |
| K8s 初次安裝、Secret／CA | [Foundation](deployment/kubernetes-staging-runbook.md) |
| K8s 掛載、原子發布與儲存故障 | [Storage Contracts](deployment/kubernetes-storage-contracts.md) |
| K8s ACTIVE 恢復與路由 | [Serving Runtime](deployment/kubernetes-serving-runtime.md) |
| K8s Worker／Control、停止確認與 ACK | [Training Runtime](deployment/kubernetes-training-runtime.md) |

CI 與重跑指令以 [實作狀態](implementation-status.md#驗證入口) 及
[workflow 原始碼](../.github/workflows/) 為準。部署命令不等同已執行的驗收結果。

## 設計與開發參考

- [HLD](pytorch-ml-system-hld.md)、[LLD](pytorch-ml-system-lld.md)：ML 設計契約；
  未交付的目標以實作狀態及後續 ADR 區分，不能把提案當作已存在的能力。
- [ADR 索引](adr/README.md)：保留決策來源與適用範圍。
- [模組規範](module-spec.md)、[底層 Backend 設計](software-design.md)、
  [通用能力教學](developer-guide.md)、[術語](glossary.md)：既有 Backend substrate。
  ML workload 擴充使用上方專用契約，不能由通用 kernel 任意選入模型。
- [微服務演進指南](microservice-contracts.md)：設計準則，不代表已拆成自治微服務。
- [通用 Backend Docker 驗證](docker-validation.md)：底層共通能力的選用檢查，
  不取代 ML Compose／K8s 完整驗收。
- [EBM 後續計畫](ebm-integration-plan.md)、[Notebook 原始 fit–gap](EBM_PoC_ML_System_Fit_Gap_2026-10-29.md)：
  保留數值對照、Temporal、Calibration、Energy／RUL 等尚未完成工作的來源。
- [原始參考資料](references/README.md)：來源原稿，非現行實作規範。

## 驗收與歷史資料

[Day-1](reviews/day1-deployment-2026-11-10.md) 與五個 K8s 工作包的
[Foundation](reviews/k8s-foundation-2026-11-17.md)、[Storage](reviews/k8s-storage-contracts-2026-11-17.md)、
[Serving](reviews/k8s-serving-runtime-2026-11-17.md)、[Training](reviews/k8s-training-runtime-2026-11-18.md)、
[Acceptance](reviews/k8s-acceptance-2026-11-18.md) 報告各自記錄受驗 source／image 及限制。
`evidence/` 的既有原始報告與雜湊維持不變；日期或測試數不是目前能力的判斷依據。

[2026-11-19 文件／程式碼對照](reviews/documentation-code-audit-2026-11-19.md) 記錄目前
維護入口、補正缺口與檢查範圍；它不取代 runtime 或正式環境驗收。

2026-11-19 已移除被取代的範本交付包、v0.2 ML 設計／圖集及舊修復流水帳。
原文可從 [清理前的固定 Git 快照](https://github.com/PenHsuanWang/ml-system-prod/tree/55dd2266395801b0dbeea40d34f4f92276fb6ac3/docs)
或 `git show 55dd226:docs/<舊檔名>` 取回；不另複製一套 archive 維護。

## 維護檢查

```sh
python scripts/check_docs.py
python scripts/update_manifest.py
python scripts/update_manifest.py --check
```

先將要交付的新增／刪除檔案加入 Git index，再更新 manifest。只列 Git-tracked
內容；`var/`、本地原稿與未提交草稿不納入清單。CI 會核對 docs、README 與 deploy
文件的連結、標題，以及 tracked manifest。新功能須同步更新對應操作／設定與限制。
