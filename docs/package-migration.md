# Package／module 搬移對照

版本：SVG 分層重構（PBP-SAD-001 1.1）。本版依使用者指定的圖實際重構，取代舊 backend/core、platform、runtime、modules 配置。HTTP paths、server:create_app、worker:broker、APP_ 設定、queue task 名稱及資料契約保留；舊 Python imports 必須修改，沒有 backend 相容 shim。

`src` 與 `bootstrap` 都是實際 Python package。從專案根目錄重新執行 `python -m pip install -c requirements.lock -e '.[dev]'`；舊環境或舊 image 需重新安裝／建置，不可只覆寫幾個 .py。

| 舊 module | 新 module |
| --- | --- |
| `backend.core.config` | `src.config.settings` |
| `backend.core.dependencies` | `src.api.dependencies` |
| `backend.core.errors` | `src.api.exceptions` |
| `backend.core.health` | `src.api.health` |
| `backend.core.observability` | `src.services.observability` |
| `backend.platform.execution.domain` | `src.domain.execution` |
| `backend.platform.execution.resources` | `src.infrastructure.locking.resources` |
| `backend.platform.execution.threads` | `src.services.concurrency` |
| `backend.platform.exports.application.service` | `src.application.exports.service` |
| `backend.platform.exports.domain.errors` | `src.domain.exports.errors` |
| `backend.platform.exports.domain.models` | `src.domain.exports.models` |
| `backend.platform.exports.domain.policy` | `src.domain.exports.policy` |
| `backend.platform.exports.domain.ports` | `src.domain.exports.ports` |
| `backend.platform.exports.infrastructure.mongo` | `src.infrastructure.persistence.exports` |
| `backend.platform.exports.presentation.router` | `src.api.exports.routers` |
| `backend.platform.identity.application.policy` | `src.application.identity.policy` |
| `backend.platform.identity.application.service` | `src.application.identity.service` |
| `backend.platform.identity.domain.errors` | `src.domain.identity.errors` |
| `backend.platform.identity.domain.models` | `src.domain.identity.models` |
| `backend.platform.identity.domain.ports` | `src.domain.identity.ports` |
| `backend.platform.identity.infrastructure.mongo_repository` | `src.infrastructure.persistence.identity` |
| `backend.platform.identity.infrastructure.security` | `src.infrastructure.security` |
| `backend.platform.identity.module` | `bootstrap.identity` |
| `backend.platform.identity.presentation.dependencies` | `src.api.auth` |
| `backend.platform.identity.presentation.router` | `src.api.identity.routers` |
| `backend.platform.identity.presentation.schemas` | `src.api.identity.schemas` |
| `backend.platform.jobs.application.registry` | `src.application.registry` |
| `backend.platform.jobs.application.service` | `src.application.jobs.service` |
| `backend.platform.jobs.domain.errors` | `src.domain.jobs.errors` |
| `backend.platform.jobs.domain.models` | `src.domain.jobs.models` |
| `backend.platform.jobs.domain.ports` | `src.domain.jobs.ports` |
| `backend.platform.jobs.infrastructure.distributed` | `src.infrastructure.messaging.distributed` |
| `backend.platform.jobs.infrastructure.memory` | `src.infrastructure.messaging.memory` |
| `backend.platform.jobs.infrastructure.redis_store` | `src.infrastructure.messaging.redis_store` |
| `backend.platform.jobs.presentation.router` | `src.api.jobs.routers` |
| `backend.platform.jobs.presentation.schemas` | `src.api.jobs.schemas` |
| `backend.platform.kernels.application.service` | `src.application.kernels.service` |
| `backend.platform.kernels.domain.models` | `src.domain.kernels.models` |
| `backend.platform.kernels.domain.ports` | `src.domain.strategy` |
| `backend.platform.kernels.presentation.router` | `src.api.kernels.routers` |
| `backend.platform.storage.mongo` | `src.infrastructure.persistence.mongo` |
| `backend.runtime.composition` | `bootstrap.wiring` |
| `backend.runtime.container` | `bootstrap.container` |
| `backend.runtime.modules` | `bootstrap.components` |
| `backend.runtime.platform_services` | `bootstrap.container` |
| `backend.runtime.resources` | `src.services.resource_manager` |
| `backend.runtime.taskiq_worker` | `bootstrap.worker` |

## 額外拆分與穩定入口

- `IdentityServices`：`src.application.identity.facade`；HTTP 認證邊界在 `src.api.auth`。
- `KernelJobHandler`：`src.application.orchestration`；四個 Service 與 handler 的公開入口為 `src.application.use_cases`。
- 健康 HTTP：`src.api.health`，集中掛載：`src.api.routers`；純 readiness：`src.services.health`。
- `src.services.lifecycle` 管理關閉順序；`bootstrap.config.load_settings` 管理設定入口。
- `src.domain.models/ports/strategy`、`shared_models`、`utils` 與 `config` 模組責任詳見 [module-spec](module-spec.md)。
- 功能 initializer 在 `bootstrap/wiring.py` 明確登錄；`ModuleDefinition/Context/Exports/Catalog` 保留為初始化 API，移到 `bootstrap.components`。
- 插件以 `PluginBinding` 明確選入，由 `bootstrap.plugin_loader` 檢查；不按 HTTP import 字串動態載入。

本次沒有新增產品資料模型或演算法，也沒有提供 queue schema/DB downgrade。新舊部署並存仍需產品驗證 handler/version 相容。原稿及先前設計取捨的歷史見 [references](references/README.md) 與 [ADRs](adr/README.md)。
