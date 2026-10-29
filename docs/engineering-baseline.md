# 工程基線與重跑規範

此文件對應 PBP-SAD-001 v1.3。實際本輪結果集中在 [validation](validation.md)；[交付狀態](handover.md) 區分已落地能力與後續部署工作。套件結構及 import namespace 維持 src.*／bootstrap.*。

## 環境與鎖定依賴

Python 宣告最低 3.11；CI 定義 3.11／3.13，實際平台與執行證據另列。其他 Python／OS／架構應先通過相同檢查，不因版本下限而宣告全部已測試。

新環境從專案根目錄執行：

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-build.lock
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
python -m pre_commit install
```

build／dev／runtime 分別使用 requirements-build.lock、requirements-dev.lock、requirements-runtime.lock；三者包含精確版本與下載 artifact hashes。requirements.lock 保留為產生式相容 constraints，使用 -c 不等於檢查完整 artifact hashes。正式安裝優先使用上述 hash locks，再以 no-deps/no-build-isolation 安裝本地已核對專案，避免再次解析未鎖定 build 依賴。

requirements-installer.in、requirements-build.in 與 pyproject.toml 是輸入；更新由唯一腳本產生：

```bash
python scripts/update_locks.py
# 明確升級允許範圍內版本時：
python scripts/update_locks.py --upgrade
```

此程序使用固定 digest 的 Linux Python 3.11 resolver image 與固定工具，在容器解析並寫回鎖定檔；需 Docker、registry／index 網路及專案寫入權限。預設保留現有相容 pins，升級須審查差異及重跑 CI matrix／package／掃描。鎖定結果覆蓋指定平台依賴解析及 artifact 完整性，不宣告所有 OS／架構或 bit-for-bit image 相同。SDK 目前仍為完整 runtime 安裝；服務連線可關閉不代表已實作 minimal extras。

## 品質與開發手冊驗證

本機提交與 CI 的 `quality` job 共用 [pre-commit 設定與操作流程](ci-precommit.md)。每份 clone 安裝一次 hook；一般 commit 依 staged 檔案選擇檢查，PR 前與 CI 執行所有 Git 追蹤檔案。Mypy 與 boundary 仍採專案範圍；自動修正後需審查再 stage。

```bash
python -m pre_commit run --all-files --show-diff-on-failure
python -m pytest -m 'not integration' --cov --cov-report=term-missing --cov-report=xml:work/coverage.xml
python scripts/verify_guide.py
```

整體 coverage 門檻為依初次完整來源基線量測設定的 85%（啟用 branch 量測，指 coverage.py 合併 line／branch 指標，非純 branch 百分比）；未因 integration adapter 未執行而移除其分母。百分比不代替授權、隔離、失敗回滾與副作用測試。guide 驗證在新副本原樣提取文件 22 個 Python 區塊，分別跑同步與背景版本的靜態／行為檢查，正式能力清單仍空白。

## 發佈產物與容器

```bash
python scripts/verify_package.py
python scripts/verify_docker.py --host-tests
```

Package verifier 建立新 build／runtime venv，hash 安裝依賴，從 sdist 建 wheel，真實非 editable 安裝 wheel；在專案外 CWD、Python isolated mode 並封鎖外部 socket 的條件下 import、啟動 disabled lifespan、檢查健康與 cleanup。正式 runtime image 會在 hash 安裝及 pip check 後移除 pip／ensurepip（包含內附 vendor 套件）；增加依賴須更新 locks 並重建，不在執行中容器安裝。基底 image 固定 digest；更新後需重跑整合與掃描。

Docker verifier 建正式 image，驗原始 CMD／非 root，再以隔離 stack 驗雙 API／雙 Taskiq、JWT／Mongo、RabbitMQ／Redis；host tests 在主機 Python 執行，外部服務來自容器。測試 fixtures 唯讀掛載，不加入正式映像。

腳本只清除自己建立的 stack，保留消毒後結果；不得將歷史 report 套用到新來源。重跑 package 時使用新的 output-dir，避免混入不同 run 的 wheel。Image 的基底與成品 digest、build source／SBOM 與依賴清單應隨 release 保存；指定 registry 前不自行推送。

## CI 與安全檢查

PR 與 main push 執行共用 pre-commit 品質規則、unit／coverage、package、guide、RabbitMQ／Redis／Mongo integration，以及獨立的 Security／Container workflows；另保留手動觸發與安全／容器每週排程。同 workflow／事件／PR 或 ref 的新執行會取消過期執行。`CI required` 彙整 CI jobs，失敗、取消或跳過均不通過；不包含另兩份 workflow。管理員另將 `python-security`、`secrets`、`image-and-broker` 設為 required checks，並依實際 check 名稱核對。各工作細節及觸發頻率以 `.github/workflows/` 為準。遠端定義存在不等於實際執行成功或已設為 required check。

Dependency audit、Bandit、gitleaks 與 Trivy 互補；掃描結果須列掃描範圍、版本、資料庫／時間與失敗門檻。預設掃描例外只准已核對的精確誤判，不允許整個 auth、docs、history 免掃。Trivy 的完整清單與高風險 gate 分開保存；未修復項目仍須列出，不能將 gate 通過描述成零漏洞。

## Runtime 與維運邊界

- disabled 支援無背景工作的多 API production；memory 限 development/test 單程序；distributed 使用受控 RabbitMQ／Redis。
- API／worker 日誌具有 service／environment／role／request／job context；一般訊息 schema_version=1，舊格式兼容。這不是 metrics exporter、trace SDK 或 audit ledger。
- API readiness 不代表 worker 有活躍 consumer；lease renewal 不等同 worker heartbeat。需要的觀測／告警、容量、HA、備份與復原依目標環境驗收。
- 普通 handler 失敗會記 failed，平台沒有通用業務 retry／持久 exactly-once。副作用、冪等、outbox、資料 migration 與補償由能力需求定義。

不為未知需求先建立 service mesh、Kubernetes framework、全域 workflow engine、SDK namespace 搬移或 deployment adapter class hierarchy。
