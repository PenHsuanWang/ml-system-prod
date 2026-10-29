# CI 與 pre-commit 操作規範

此文件說明本機提交檢查與 GitHub Actions 的共同規則。
設定以 `.pre-commit-config.yaml`、`pyproject.toml`、hash locks 與 `.github/workflows/` 為準；
實際執行結果另見 [驗證紀錄](validation.md)。

## 每份 clone 的首次安裝

從專案根目錄建立固定名稱的 `.venv`：

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-build.lock
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
python -m pre_commit validate-config
python -m pre_commit install
python -m pre_commit run --all-files --show-diff-on-failure
```

Python 最低版本為 3.11，CI 測試矩陣為 3.11／3.13。
hash locks 以 Linux 解析，macOS 另作本機驗證；Windows 團隊使用 WSL2 或 Linux 容器，
不將原生 Windows 視為已驗證平台。

所有 hooks 為 `repo: local`，直接執行 `.venv/bin/python`，共用 hash 鎖定的 dev 依賴。
不另外下載 remote hook repository 或建立另一組工具版本。
請保留 `.venv` 路徑；Git GUI 不需先啟用 shell 的虛擬環境，但該環境必須存在且依賴完整。

`pre_commit install` 將 hook 安裝至這一份 clone 的 Git hooks 目錄；
提交設定檔不會自動替其他開發者安裝 hook。
重建環境後可重新執行安裝命令。
pre-commit 的安裝、本機 hook 與執行方式見 [官方文件](https://pre-commit.com/)。

## 日常提交

1. 修改程式與文件，加入本次要提交的檔案。
2. 執行 `git commit`；hook 依 staged 檔案選擇需要執行的檢查。
3. 若 Ruff、空白或檔尾檢查修改檔案，先檢視 diff，再將確定要提交的修改重新加入暫存區並重試。
4. PR 前執行全檔檢查，及變更所需的測試、package／整合驗證。

```bash
python -m pre_commit run --all-files --show-diff-on-failure
python -m pytest -m 'not integration' --cov --cov-report=term
```

`--all-files` 檢查 Git 追蹤的檔案；新檔案需先加入 Git。
自動修正不會替你 stage 檔案，也不代表未執行的單元或整合測試通過。
有部分暫存的檔案時，特別檢查修正前後的差異，避免把其他未完成修改一併提交。

## 檢查範圍

| Hook | 規則 |
| --- | --- |
| `forbid-local-env` | 拒絕提交本機 `.env`／`.env.*`；允許 `.env.example` |
| `check-merge-conflict` | 檢查文字檔中的未解決合併標記 |
| `detect-private-key` | 檢查文字檔中的私鑰格式標記 |
| `check-yaml`、`check-toml`、`check-json` | 驗證對應設定／資料檔語法 |
| `trailing-whitespace`、`end-of-file-fixer` | 修正尾端空白與檔尾；保留 Markdown 的雙空白換行 |
| `ruff`、`ruff-format` | Python lint、自動修正與格式化 |
| `mypy` | 相關來源／設定／鎖定檔改動時，檢查完整正式程式入口與 packages |
| `architecture-boundaries` | 每次執行都檢查分層依賴，禁止越界 import |

Mypy 與架構檢查採專案範圍，並非僅檢查提交檔案。
測試、coverage、package、guide 與真實服務整合仍由 CI 及明確的驗證命令執行，
避免每次 commit 都建立容器。

`docs/references/` 與 `docs/evidence/` 的歷史原稿／證據不由尾端空白與檔尾 hooks 改寫。
這兩個目錄也排除 JSON 語法 hook，因歷史 CLI 報告可能以 `.json` 名稱保存 JSONL。
排除只限這三個 hooks；私鑰與合併標記檢查仍掃描其文字內容。
新增證據時仍須依實際格式驗證，不能以歷史排除取代正確性檢查。

本機私鑰檢查不涵蓋所有 token、密碼或 Git 歷史。
CI 的 gitleaks 另以版本控制的 `.gitleaks.toml` 掃描完整取得的 Git 歷史；
不得刪除此設定檔或以整個目錄免掃方式規避檢查。

## CI 工作與合併門檻

三份 workflow 都在 PR、推送 `main` 與手動觸發時執行；
Security／Container 另有每週排程。
同一 workflow、事件及 PR／ref 的新執行會取消過期執行，
避免同一變更持續佔用 runner。功能分支的單獨 push 不觸發這三份 workflow，開 PR 後會執行。

| Workflow／job | 用途 |
| --- | --- |
| CI／`workflows` | actionlint 驗證 workflow |
| CI／`quality` | 建立 `.venv`，執行與本機相同的所有 pre-commit hooks |
| CI／`unit` | Python 3.11／3.13 單元與 coverage；3.13 另驗開發手冊 |
| CI／`integration` | 真實 RabbitMQ／Redis／MongoDB 整合 |
| CI／`package` | Python 3.11／3.13 sdist、wheel、非 editable 安裝與 metadata 驗證 |
| CI／`CI required` | 彙整上述 CI jobs，任何失敗、取消或跳過均不通過 |
| Security／`python-security` | hash 依賴漏洞與 Bandit 靜態掃描 |
| Security／`secrets` | gitleaks Git 歷史掃描 |
| Container／`image-and-broker` | 正式 image 與服務整合、SBOM、容器漏洞掃描 |

`CI required` 只彙整同一份 CI workflow，未包含另兩份 workflow。
組織管理員應在目標分支的 ruleset／branch protection 選取以下 required checks：

- `CI required`
- `python-security`
- `secrets`
- `image-and-broker`

選取前先讓實際 PR 產生各 check，依 GitHub 顯示的 check 名稱核對。
這些 workflow 定義不會自行設定遠端 branch protection，也不代表遠端已執行成功。
檢查失敗時先查看 job 與消毒後 artifact；掃描門檻與限制見 [工程基線](engineering-baseline.md)。


本機 hook 可以被略過，團隊應以受保護分支的 CI 為合併門檻。若未來啟用 GitHub merge queue，須先替 workflows 加入並驗證 `merge_group` 事件；目前未設定此事件。

## 更新工具版本

工具版本統一在 `pyproject.toml` 的 dev 依賴及 canonical lock 流程維護：

```bash
python scripts/update_locks.py
# 明確升級允許範圍內的版本：
python scripts/update_locks.py --upgrade
```

此流程需要 Docker 與依賴 index／registry 存取。
不要手改 lock hashes；更新後重新 hash 安裝 dev 依賴，並重跑 hooks、CI matrix 與安全掃描。
本專案使用全部 local hooks，`pre-commit autoupdate` 不負責更新這些工具。
新規則、例外或工具版本變更均需附原因、差異及對應驗證結果供審查。
