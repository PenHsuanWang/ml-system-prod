"""Recompute baseline source line references; all outputs stay under docs/architecture."""
from pathlib import Path
import json

import application_views
import deployment_views
import message_sources
import sequence_views
R=Path(__file__).resolve().parents[3]; D=R/'docs/architecture'
GROUPS={
'E01':('HTTP／identity／admission',[
('bootstrap/ml_api.py','def create_app('),('src/api/ml_http.py','def submit('),('src/api/ml_http.py','def mount_management('),('src/api/ingestion/routers.py','def submit('),('src/application/identity/service.py','class IdentityService:'),('src/api/identity/routers.py','@router.post("/login"'),('src/infrastructure/ml_identity.py','class MongoActorResolver')]),
'E02':('Composition／dependency direction',[
('bootstrap/ml_wiring.py','def build_components('),('bootstrap/ml_wiring.py','def supervisor('),('bootstrap/ml_training_wiring.py','def training_executor('),('bootstrap/ml_serving_wiring.py','def endpoint_resolver('),('bootstrap/components.py','class ModuleDefinition:'),('bootstrap/components.py','class ModuleCatalog:'),('bootstrap/wiring.py','def build_modules('),('src/application/training/ports.py','class TrainingRepository'),('src/domain/training/models.py','class JobState'),('src/domain/training/workloads.py','class WorkloadContract')]),
'E03':('SQL authority／Outbox／delivery',[
('src/application/training/service.py','def submit('),('src/infrastructure/persistence/ml_training.py','def submit('),('src/infrastructure/persistence/ml_training.py','def validate_delivery('),('src/infrastructure/persistence/ml_database.py','class Database:'),('src/application/training/outbox.py','class OutboxRelay:'),('src/infrastructure/messaging/ml_rabbitmq.py','class RabbitMQCommandPublisher'),('src/infrastructure/messaging/ml_topology.py','def build_training_broker('),('src/application/training/delivery.py','async def acknowledge_terminal(')]),
'E04':('Compute／registration／evaluation／recovery',[
('src/application/training/supervisor.py','def execute('),('src/application/training/supervisor.py','def monitor('),('src/application/training/supervisor.py','def finalize('),('src/application/training/supervisor.py','def _finalize('),('bootstrap/ml_worker.py','def build_broker('),('bootstrap/ml_training_runner.py','def main('),('src/infrastructure/execution/local_process.py','def launch('),('src/infrastructure/mlflow/tracking.py','class LocalExperimentTracker'),('src/infrastructure/mlflow/tracking.py','class MLflowExperimentTracker'),('src/infrastructure/serving/evaluation.py','class WorkloadBundleEvaluator')]),
'E05':('Ingestion／RawSnapshot／DatasetBuilder',[
('src/application/ingestion/service.py','def submit('),('src/application/ingestion/supervisor.py','def run_once('),('src/application/datasets/builder.py','class EBMDatasetPreparationService:'),('src/plugins/features/ebm_dataset_builder_v1.py','splits_by_lot ='),('src/infrastructure/ingestion/adapters/databricks_client.py','class DatabricksSQLClient:'),('src/infrastructure/ingestion/storage/raw_snapshot_writer.py','def directory_fd('),('bootstrap/ingestion_cli.py','def main('),('bootstrap/ingestion_wiring.py','class IngestionSettings:')]),
'E06':('Governance／deployment／fencing',[
('src/application/model_registry/service.py','def approve('),('src/infrastructure/persistence/ml_training.py','def finish('),('src/infrastructure/persistence/ml_deployments.py','def create('),('src/application/deployments/service.py','def rollback('),('src/application/deployments/service.py','class DeploymentController:'),('bootstrap/ml_cli.py','def control(')]),
'E07':('Gateway／Serving／inference',[
('bootstrap/ml_gateway.py','async def predict('),('bootstrap/ml_gateway.py','async def ready('),('bootstrap/ml_serving.py','def create_app('),('bootstrap/ml_serving.py','def main('),('src/infrastructure/deployment/local_serving.py','def _launch('),('src/infrastructure/serving/materializer.py','class BundleMaterializer:'),('src/infrastructure/serving/bounded_prediction.py','class BoundedPredictionExecutor:'),('src/infrastructure/serving/bounded_ebm_prediction.py','class BoundedEBMPredictionExecutor:'),('src/application/prediction/service.py','class PredictionService:')]),
'E08':('Docker／Compose reference topology',[
('compose.ml.yaml','x-ml-environment:'),('compose.ml.yaml','x-ml-app:'),('compose.ml.yaml','  postgres:'),('compose.ml.yaml','  mlflow:'),('compose.ml.yaml','  migrate:'),('compose.ml.yaml','  supervisor:'),('compose.ml.yaml','  gateway:'),('compose.ml.ingestion.yaml','  ingestion-worker:'),('compose.ml.databricks.yaml','secrets:'),('compose.ml.databricks-token.yaml','services:'),('compose.ml.acceptance.yaml','  acceptance:'),('compose.yaml','services:'),('Dockerfile','FROM '),('Dockerfile.ml','FROM python:3.12-slim AS api'),('Dockerfile.ml','FROM api AS training'),('Dockerfile.ml','FROM ebm AS production'),('Dockerfile.mlflow','ENTRYPOINT'),('Dockerfile.minio','FROM runtime AS server')]),
'E09':('Host configuration／startup／platform limits',[
('bootstrap/ml_config.py','class MLSettings:'),('bootstrap/ml_config.py','def from_env('),('bootstrap/ml_cli.py','def migrate('),('bootstrap/ml_cli.py','async def relay('),('bootstrap/ml_cli.py','def control('),('bootstrap/ml_cli.py','def demo('),('bootstrap/ml_cli.py','def demo_ebm('),('bootstrap/ml_cli.py','def main('),('src/infrastructure/execution/local_process.py','def process_identity('),('src/infrastructure/execution/local_process.py','def inspect('),('src/config/settings.py','class Settings('),('pyproject.toml','[project.optional-dependencies]'),('worker.py','broker ='),('server.py','def create_app('),('README.md','## Real local training'),('docs/configuration-reference.md','## Profile')]),
'E10':('External production profile／secrets',[
('compose.ml.external-mlflow.yaml','x-runtime-environment:'),('compose.ml.external-mlflow.yaml','x-runtime:'),('compose.ml.external-mlflow.yaml','services:'),('bootstrap/deployment_entrypoint.py','REQUIRED ='),('bootstrap/deployment_entrypoint.py','def _read_configuration('),('bootstrap/deployment_entrypoint.py','def load_runtime_environment('),('docs/deployment/day1-deployment-runbook.md','# Day-1'),('docs/configuration-reference.md','`docker compose exec`')]),
'E11':('Implemented Kubernetes profiles／qualification boundaries',[
('deploy/k8s/base/workloads.yaml','  name: ml-management'),('deploy/k8s/base/workloads.yaml','  name: ml-supervisor'),('deploy/k8s/base/workloads.yaml','  name: ml-control'),('deploy/k8s/base/workloads.yaml','  name: ml-ingestion-worker'),('deploy/k8s/base/services.yaml','  name: ml-gateway'),('deploy/k8s/base/storage.yaml','kind: PersistentVolumeClaim'),('deploy/k8s/base/networkpolicies.yaml','kind: NetworkPolicy'),('deploy/k8s/overlays/minikube-training/training-runtime.yaml','  name: ml-supervisor'),('deploy/k8s/overlays/minikube-serving/control-api.yaml','  name: ml-control'),('deploy/k8s/infrastructure/minikube/mlflow.yaml','kind: Deployment'),('src/infrastructure/deployment/kubernetes_serving.py','class KubernetesServingExecutor'),('src/infrastructure/execution/pod_process.py','class PodProcessExecutor'),('bootstrap/ml_training_guardian.py','def supervise('),('docs/adr/017-kubernetes-runtime-boundaries.md','## Decision'),('docs/adr/018-kubernetes-storage-contracts.md','## Decision'),('docs/adr/019-kubernetes-serving-runtime.md','## Decision'),('docs/adr/020-pod-training-runtime.md','## Decision'),('docs/adr/021-kubernetes-acceptance.md','## Decision')]),
'E12':('Verification／observability／static limits',[
('scripts/check_boundaries.py','LAYER_DEPENDENCIES:'),('scripts/verify_ml_acceptance.py','def main('),('tests/ml/test_training_contracts.py','def test_concurrent_acceptance'),('tests/ml/test_model_governance.py','def test_approval_pins_exact_evidence'),('tests/ml/test_pod_training_runtime.py','async def test_ack_requires_terminal_sql'),('.github/workflows/ml-ci.yml','  contracts:'),('bootstrap/ml_telemetry.py','def configure_telemetry('),('src/infrastructure/ml_tracing.py','W3C tracing')]),
'E13':('Backend／CPU baseline decisions',[
('docs/adr/README.md','## ADR-008'),('docs/adr/README.md','## ADR-009'),('docs/adr/README.md','## ADR-010'),('docs/adr/014-implementation-baseline.md','The upstream source'),('docs/adr/014-implementation-baseline.md','1. Freeze'),('docs/adr/014-implementation-baseline.md','2. Use'),('docs/adr/014-implementation-baseline.md','3. PostgreSQL'),('docs/adr/014-implementation-baseline.md','4. Keep')]),
'E14':('Approved providers／workloads／initialization',[
('docs/adr/016-databricks-batch-ingestion.md','## Decision'),('src/domain/training/workloads.py','APPROVED_WORKLOADS ='),('bootstrap/ml_training_runner.py','TRAINING_HANDLERS ='),('bootstrap/init_cli.py','def main('),('docs/deployment/interactive-setup.md','# '),('src/plugins/models/ebm_cnn_v1.py','def build_ebm_cnn(')]),
'E15':('Retained evidence／deferred scope',[
('docs/implementation-status.md','## 已交付'),('docs/implementation-status.md','## 尚未完成'),('docs/ebm-implementation-status.md','## 已實作'),('docs/ebm-implementation-status.md','尚未提供跨 Job'),('docs/ebm-integration-plan.md','# '),('docs/evidence/k8s-acceptance-2026-11-18/qualification.json','{')])}
BASELINE = '8c9023b00828000287e169ac760afe57102d7353'
SOURCES = {
    **application_views.SOURCES, **deployment_views.SOURCES, **message_sources.SOURCES,
    '01-system-context': {
        'input-0': ('src/api/ml_http.py','@training_router.post("/jobs", status_code=202)'),
        'input-1': ('src/api/ml_http.py','@registry_router.post("/bindings/{binding_id}/approvals"'),
        'input-2': ('bootstrap/ingestion_cli.py','commands = parser.add_subparsers'),
        'input-3': ('bootstrap/ml_gateway.py','@app.post("/api/v1/prediction/predict")'),
        'integration-0': ('src/application/ingestion/supervisor.py','def run_once('),
        'integration-1': ('src/infrastructure/mlflow/tracking.py','self.client.log_artifacts(run_id'),
        'integration-2': ('compose.ml.yaml','x-ml-environment:'),
        'integration-3': ('bootstrap/ml_telemetry.py','provider.add_span_processor(BatchSpanProcessor('),
    }
}


def locate(spec):
    path,needle=spec
    lines=(R/path).read_text().splitlines()
    return dict(path=path,line=next(i for i,line in enumerate(lines,1) if needle in line),needle=needle)


def source_link(ref):
    path,line=ref['path'],ref['line']
    return f'[{path}:{line}](../../{path}#L{line})'


model=json.loads((D/'diagram-model.json').read_text())
assert model['source_commit']==BASELINE
index={'source_commit':BASELINE,'groups':{},'relationships':{}}
for module in (application_views,deployment_views,sequence_views):
    for group,path,needle in module.EXTRA_EVIDENCE:
        GROUPS[group][1].append((path,needle))
out=[
    '# 圖面與原始碼對照','',f'基準 `master@{BASELINE}`。每條箭頭記錄起點、終點、語義與可定位的原始碼。群組索引補充同一操作跨檔案的實作與 ADR。','',
    '拓撲圖的框線標示所屬範圍，箭頭顏色與線型見各圖圖例。交叉線各自通往箭頭所指的元件。時序圖每一欄代表固定參與者，由上往下閱讀；條件式訊息僅適用於標示的分支。','',
    '## 圖面索引','',
]
scopes=[
    'C4 L1。使用者、核准來源、外部服務與產品邊界。',
    'C4 L2。以 local-process execution backend 與 MLflow 為例。訓練與 Serving 是按需啟動的角色，共享資源各有唯一節點。',
    'C4 L3。單一 Management executable 中處理 training admission 的元件。自動 startup wiring、source-level type import 與 request-time method call 分別標記。',
    '時序。由同一個訓練請求追蹤 SQL、broker、worker 與 artifact 的成功及條件分支。Approval 與 deployment 各有人工入口。',
    '時序。Gateway 與 Serving 分別驗證身份。依序檢查 readiness、成功回應的 provenance、最後一次 SQL route。',
    'Native。上方是 CLI demo，下方是同一 Linux 主機的多程序組合與須可達的依賴。完整 native macOS 行為仍待驗證。',
    'Compose。框線標示 project network、Docker container 與共享 network namespace。以 compose.ml.yaml 為主，疊加角色明確標記。',
    'Kubernetes。觀察指定 Minikube overlay 的 Pod 與 container 邊界。Serving 資源依 Deployment identity 建立；資格限制見第 8 章。',
]
for item,scope in zip(model['diagrams'],scopes,strict=True):
    name=item['file']
    key=Path(name).stem
    nodes={n['id']:n for n in item['nodes']+item['groups']}
    citations=SOURCES[key]
    assert set(citations)=={r['id'] for r in item['relationships']}, name
    out += [f'### {name}','',scope,'',f'[開啟獨立 SVG](diagrams/{name})','']
    if item['groups']:
        for b in item['groups']:
            members=[n['title'] for n in nodes.values() if n.get('parent')==b['id']]
            if members:
                out.append(f"- **{b['title']}**　"+'、'.join(members)+'。')
        out.append('')
    if item.get('phases'):
        out += ['時間由上而下，表中訊息編號沿用圖面順序。','']
    out += ['| ID | 起點 → 終點 | 操作 | 語義 | 直接來源與延伸證據 |','| --- | --- | --- | --- | --- |']
    rows=index['relationships'][name]={}
    for rel in item['relationships']:
        ref=locate(citations[rel['id']])
        rows[rel['id']]={**ref,'source':rel['source'],'target':rel['target'],'groups':rel['evidence']}
        more='、'.join(f'[{g}](#{g.lower()})' for g in rel['evidence'])
        target=nodes[rel['target']]['title']
        source=nodes[rel['source']]['title']
        out.append(f"| `{rel['id']}` | {source} → {target} | {rel['label']} | {rel['kind']} | {source_link(ref)} · {more} |")
    out.append('')
for key,(title,refs) in GROUPS.items():
    index['groups'][key]=[]
    out += [f'## {key}','','**'+title+'**','','| Exact source / line | Named symbol or declaration |','| --- | --- |']
    for source in dict.fromkeys(refs):
        ref=locate(source)
        index['groups'][key].append(ref)
        out.append(f"| {source_link(ref)} | `{ref['needle']}` |")
    out.append('')
out += ['## 歷史與 PR 證據','','以 `git log --first-parent --reverse --format="%h %ad %s" --date=short` 與 `git show --no-patch --format=full <commit>` 取得本機 commit／merge messages。未查遠端 PR 討論；不把 merge title 延伸成未知作者動機。','', '| Commit | PR / contribution |','| --- | --- |']
for sha,desc in [('a88f6ff','Initial import；ADR-014 已存在'),('edc1400','EBM training／serving'),('122c468','PR #1 governed ingress'),('72dc1c3','PR #2 access token alternative'),('97301cf','PR #3 DatasetBuilder'),('06ddc3a','PR #4 EBM end-to-end'),('ce81327','PR #5 approved workload dispatch'),('79cc22c','Immutable workload registry refactor'),('f2040ed','PR #7 K8s foundation'),('3150c04','PR #8 storage'),('a5c3847','PR #9 native Serving'),('336615e','PR #10 Pod training'),('55dd226','PR #11 governed acceptance'),('6b536ea','PR #12 documentation'),('8c9023b','PR #13 initialization')]:
    out.append(f'| [`{sha}`](https://github.com/PenHsuanWang/ml-system-prod/commit/{sha}) | {desc} |')
(D/'diagram-evidence.md').write_text('\n'.join(out)+'\n');(D/'evidence-index.json').write_text(json.dumps(index,indent=2,ensure_ascii=False)+'\n')
print(f"Indexed {sum(map(len,index['groups'].values()))} group references and "
      f"{sum(map(len,index['relationships'].values()))} direct relationship references")
