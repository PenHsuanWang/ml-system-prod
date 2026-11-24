# Documentation validation

Baseline：`master@8c9023b00828000287e169ac760afe57102d7353`；2026-11-24。

## 範圍與結果

文件 artifact 驗證 **PASS**。本次只寫入 `docs/architecture/`。沒有安裝 dependencies、修改 application／runtime／deployment configuration、啟動服務、連 production、commit／push 或執行 runtime acceptance。

| 驗證項目 | 結果 | 具體證據／限制 |
| --- | --- | --- |
| 八張必要 SVG | PASS | `01`～`08` 實際檔案存在；K8s 由目前 manifests／adapters 支持 |
| XML／standalone namespace | PASS | ElementTree 解析全部 8 張；root 為 SVG namespace；title／desc／viewBox 正確 |
| 自包含性 | PASS | 無 external image、script 或 foreignObject；標準 SVG text／rect／path／marker |
| 圖面幾何 | PASS | node 不重疊／超界；connector orthogonal 且不穿過非 boundary node；Arial text metrics 無 box-edge overflow |
| Relation evidence | PASS | 75 個非 legend relations 都對應有效 evidence IDs |
| Source references | PASS | 146 個 path／line／named declaration 與 baseline source 相符 |
| Guidebook 結構 | PASS | 12 個編號主章；code fences 配對；內容為繁體中文，保留 source identifiers |
| Markdown links／images／anchors | PASS | 371 個 local links；全部 resolve，含八張圖與 source line anchors |
| SVG rendering | PASS | 現有 `rsvg-convert 2.61.3` 渲染 8／8；未安裝工具 |
| Visual inspection | PASS | 觀察全部 rendered diagrams；修正後的 03／05／07／08 再檢視；無可見 clipping／overflow／label overlap |
| Source semantics review | PASS（人工 static review） | 對照 source／ADRs 檢查 gates、runtime ownership 與資料方向；不是自動證明所有架構語義 |
| Whitespace／scope | PASS | validator 包含 untracked files 的 whitespace／newline 檢查；Git scope 只允許 docs/architecture |
| Runtime／production verification | INCONCLUSIVE | 未執行 app、tests、Compose、K8s 或外部服務；沒有新的 runtime 成功宣告 |

## 實際執行命令

所有命令從 repository root 執行，最後版本均 exit `0`：

```sh
git branch --show-current
git rev-parse HEAD
git status --short
python3 -B docs/architecture/tools/build_diagrams.py
python3 -B docs/architecture/tools/build_evidence.py
python3 -B docs/architecture/tools/validate_docs.py
python3 -B docs/architecture/tools/render_diagrams.py --output-dir /private/tmp/ml-architecture-preview
rsvg-convert --version
git diff --check
git diff --stat
git status --short --untracked-files=all
```

`validate_docs.py` 最終輸出：8 SVG、146 source references、371 local links、75 relationships；warnings／errors 均為空。`git diff --stat` 無 tracked-file 修改；new deliverables 由 `git status --untracked-files=all` 與 validator 檢查。單獨 `git diff --check` 不涵蓋 untracked files，因此另外檢查文件 newline／trailing whitespace。

初輪檢查發現 component titles 超出 box 及 validation 文件尚未建立；已縮短圖面標題並補上本文件，重跑通過。人工 source review 另修正 inference provenance／route reread 次序、Gateway／Control network 關係，以及 Management Pod 的 Service 語義。Vertical connector labels 移至線旁，避免文字疊在線上。

## Rendering 與 visual inspection 紀錄

Renderer：librsvg `2.61.3`，既有 Cairo／Pango／HarfBuzz／fontconfig。以寬 1500 px 產生 PNG；文件交付保留可縮放 SVG，preview PNG 存在 temporary directory，不是 runtime evidence。

| SVG | 最終 PNG 尺寸 | 視覺檢視 |
| --- | --- | --- |
| `01-system-context.svg` | 1500 × 1080 | PASS；product boundary／external actors 清楚 |
| `02-container-architecture.svg` | 1500 × 1402 | PASS；relationship lanes／shared endpoints 可辨識 |
| `03-component-architecture.svg` | 1500 × 1110 | PASS；ports／implements direction／injection label 重新檢視 |
| `04-training-sequence.svg` | 1500 × 2298 | PASS；按號碼由上而下；registration／evaluation／manual gates 分開 |
| `05-inference-sequence.svg` | 1500 × 1673 | PASS；response provenance 在 route reread 之前 |
| `06-local-runtime.svg` | 1500 × 1298 | PASS；demo 與完整 host scope 分開說明 |
| `07-docker-compose-runtime.svg` | 1500 × 1569 | PASS；Control role、Gateway shared network、one-shot／external profiles 明列 |
| `08-kubernetes-runtime.svg` | 1500 × 1444 | PASS；Pod／process／Service／native resource 與 qualification gaps 分開 |

已透過 image viewer 檢視 contact sheets 與修正圖；最後單獨檢視 full-resolution component 圖。自動 text metrics 使用現有 Pillow 與系統 Arial；其他機器無此 font metrics 時 validator 會明列 warning，不能據此省略 rendering／visual inspection。

## 尚未解決的架構問題

- Full native host profile 的 OS／Python／dependencies／shutdown acceptance contract。
- Compose Worker／Control 跨 PID／hostname identity 的 physical recovery 與 stop budget qualification。
- Enterprise MLflow／Databricks 權限、資料／artifact access 與 TLS 資格。
- 公司 cluster／CSI、multi-node／partition fencing、backup／restore、capacity／SLO。
- 代表性 EBM quality policy 與實際產品 Calibration／Energy／RUL 接受條件。

這些是原有實作／驗收邊界，不是本文件工作失敗；Guidebook 已明確區分 IMPLEMENTED、DOCUMENTED、PROPOSED、UNKNOWN。處理任何 production 或程式改動需另立工作範圍。
