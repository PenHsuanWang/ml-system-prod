# Specification Quality Checklist — ML-SPEC-001

> 文件性質：Draft review checklist，非已執行的測試報告。版本 0.2（2026-10-29）：文字與跨文件引用已收斂，Owner 核准、實測與正式數值仍待完成。

## Content quality

- [x] 限定為 Durable Training Jobs capability；另列非本次範圍。
- [x] 以 observable behavior、使用者目標與狀態不變量為核心，不包含 SQL 與具體類別實作。
- [x] 有 User Stories、正常、失敗、重送、並行、取消及故障恢復情境。
- [x] 納入權限、租戶、冪等與狀態不變量。

## Must be reviewed before approval

- [ ] 提交 API 的所有欄位、回應與錯誤語意已由 API Owner 與 ML Consumer 核准。
- [ ] Request Fingerprint 與不可變 TrainingSpec Digest 已各自定義正規化／版本政策，並有等價請求、預設變動及版本變動測試。
- [ ] `Idempotency-Key` 保留期限已由 Product／Platform 核准、對外契約已鎖定（文件內 24 小時目前僅為提案）。
- [ ] Job／Attempt／取消狀態的合法轉換、競爭順序與可見性已核對。
- [ ] 權限矩陣與不存在資源的錯誤政策已核准。
- [ ] Job Queue Deadline、Process Stop/Grace、Retry、Recovery SLA 已核准。
- [ ] Broker ACK eligibility 具備真 Receiver / DB integration 測試設計。
- [ ] 與當前 Template 的 API module prefix 與 DI／角色能力無衝突。
- [ ] 所有 Requirement 有自動化或人工驗收依據及負責者。
- [ ] HLD／LLD／Spec 三者的必要差異已用 ADR 或設計更新收斂。

## Decision

**Current:** Draft; can be used for review and planning preparation, **not approved for production**.
