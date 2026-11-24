"""Source-backed message order for the training and prediction interaction views."""

SUPERVISOR = 'src/application/training/supervisor.py'
GATEWAY = 'bootstrap/ml_gateway.py'

EXTRA_EVIDENCE = [
    ('E03', 'src/application/training/outbox.py', 'await asyncio.to_thread(self.repository.retry, item)'),
    ('E03', 'src/infrastructure/persistence/ml_outbox.py', 'state="DEAD" if row["tries"] >= 20'),
    ('E03', 'src/infrastructure/messaging/ml_rabbitmq.py', 'if not isinstance(confirmation, Basic.Ack)'),
    ('E03', 'src/infrastructure/messaging/training_ack.py', '"ml:ack:" + snapshot.command_id'),
    ('E03', 'src/application/training/delivery.py', 'await store.persist(snapshot)'),
    ('E03', 'src/application/training/delivery.py', 'await ack()'),
    ('E04', 'src/infrastructure/persistence/ml_training.py', 'def prepare_execution('),
    ('E04', SUPERVISOR, 'self.repository.prepare_execution(claim,'),
    ('E04', SUPERVISOR, 'self.executor.launch(handle)'),
    ('E04', 'src/plugins/training/pytorch_trainer.py', 'atomic_json(directory/"result.json", result)'),
    ('E04', 'src/plugins/training/ebm_trainer_v1.py', 'atomic_json(directory / "result.json", result)'),
    ('E04', 'src/infrastructure/execution/local_process.py', 'Path(handle.directory)/"result.json"'),
    ('E04', SUPERVISOR, 'version, artifact_ref = self.tracker.register('),
    ('E04', SUPERVISOR, 'binding = self.registry.save_binding('),
    ('E04', SUPERVISOR, 'evaluation = self.evaluator.evaluate('),
    ('E04', SUPERVISOR, 'evaluation = self.registry.save_evaluation(evaluation)'),
    ('E04', SUPERVISOR, 'self.repository.finish(claim, binding.binding_id, evaluation.evaluation_id)'),
    ('E06', 'src/api/ml_http.py', '@registry_router.post("/bindings/{binding_id}/approvals", status_code=201)'),
    ('E06', 'src/api/ml_http.py', '@deployment_router.post("", status_code=202)'),
    ('E06', 'bootstrap/ml_cli.py', 'rows = connection.execute(select(deployments).where('),
    ('E06', 'src/infrastructure/deployment/local_serving.py', 'self._processes[deployment.deployment_id] = subprocess.Popen('),
    ('E06', 'src/infrastructure/deployment/local_serving.py', 'response = httpx.get(metadata["address"]+"/health/ready"'),
    ('E06', 'src/application/deployments/service.py', 'if not self.executor.ready(deployment):'),
    ('E06', 'src/application/deployments/service.py', 'self.repository.activate(deployment)'),
    ('E06', 'src/infrastructure/persistence/ml_deployments.py', 'routes.c.revision == deployment.expected_route_revision).values('),
    ('E07', 'src/infrastructure/ml_identity.py', 'session = self.db.identity_sessions.find_one('),
    ('E07', 'src/infrastructure/deployment/endpoints.py', 'return f"http://{resource_name(deployment)}.'),
    ('E07', 'src/infrastructure/deployment/endpoints.py', 'address = json.loads((self.root / deployment.deployment_id / "instance.json")'),
    ('E07', 'src/domain/deployments/identity.py', 'def matches_identity('),
    ('E07', 'src/infrastructure/persistence/ml_deployments.py', 'def route(self, tenant: str, endpoint: str) -> Route:'),
    ('E07', 'src/domain/deployments/models.py', 'class Route:'),
    ('E07', 'bootstrap/ml_serving.py', 'result = asdict(state["prediction"].predict('),
    ('E07', 'bootstrap/ml_serving.py', 'result["provenance"].update(spec_digest=trusted_digest'),
    ('E07', GATEWAY, 'address = state["resolver"].resolve(deployment)'),
    ('E07', GATEWAY, 'if response.is_success and ('),
    ('E07', GATEWAY, 'current = await asyncio.to_thread('),
    ('E07', GATEWAY, 'if current != route or response.is_redirect:'),
]


TRAINING = {
    'id': '04-training-sequence',
    'kind': 'sequence',
    'title': 'Training, delivery and release decisions',
    'subtitle': 'Reference worker with local compute and MLflow. Each guarded exit applies only when its condition holds.',
    'participants': [
        ('scientist', 'Scientist', 'ml:train'),
        ('reviewer', 'Reviewer', 'ml:model:approve'),
        ('deployer', 'Deployer', 'ml:deploy'),
        ('api', 'Management', 'HTTP API'),
        ('control', 'Control', 'reconciler'),
        ('serving', 'Serving', 'child process'),
        ('sql', 'SQL', 'business state'),
        ('relay', 'Relay', 'outbox poller'),
        ('rabbit', 'RabbitMQ', 'command queue'),
        ('worker', 'Supervisor', 'Taskiq worker'),
        ('compute', 'Compute', 'child process'),
        ('tracker', 'MLflow', 'model registry'),
        ('redis', 'Redis', 'ACK evidence'),
    ],
    'phases': [
        ('Accept a training request', [
            ('scientist', 'api', 'POST /api/v1/training/jobs + Idempotency-Key', 'sync', 'E01'),
            ('api', 'sql', 'Commit job, attempt, key, outbox and audit together', 'data', 'E03'),
            ('api', 'scientist', '202 · job_id and status_url', 'return', 'E01'),
        ]),
        ('Publish the durable command', [
            ('relay', 'sql', 'Lease pending outbox row', 'observe', 'E03'),
            ('relay', 'rabbit', 'Publish persistent command_id', 'async', 'E03'),
            ('relay', 'sql', 'If publish fails → retry with backoff or mark DEAD', 'branch', 'E03'),
            ('rabbit', 'relay', 'On success · publisher confirm', 'return', 'E03'),
            ('relay', 'sql', 'Mark leased command PUBLISHED', 'data', 'E03'),
            ('rabbit', 'worker', 'Deliver command · at least once', 'async', 'E03'),
        ]),
        ('Claim the attempt and launch compute', [
            ('worker', 'sql', 'Match durable intent and claim attempt', 'sync', 'E03'),
            ('sql', 'worker', 'If terminal → skip fit and go to ACK gate', 'branch', 'E03'),
            ('worker', 'sql', 'Persist prepared launch identity in execution_ref', 'data', 'E04'),
            ('worker', 'compute', 'Spawn approved training process', 'launch', 'E04'),
            ('worker', 'sql', 'If physical stop is uncertain → record reconciliation state', 'branch', 'E04'),
            ('compute', 'compute', 'Fit and write frozen bundle, metrics and result.json', 'data', 'E04'),
            ('compute', 'worker', 'After exit · result read from execution directory', 'return', 'E04'),
        ]),
        ('Register and evaluate the exported bundle', [
            ('worker', 'tracker', 'Log metrics and register frozen artifact', 'data', 'E04'),
            ('tracker', 'worker', 'Registry version and artifact_ref', 'return', 'E04'),
            ('worker', 'sql', 'Save immutable ModelBinding', 'data', 'E04'),
            ('worker', 'worker', 'Evaluate exact artifact on held-out data', 'sync', 'E04'),
            ('worker', 'sql', 'If evaluator raises → save ERROR; leave recovery pending', 'branch', 'E04'),
            ('worker', 'sql', 'Save Evaluation, then finish job with evidence IDs', 'data', 'E04'),
        ]),
        ('Acknowledge a terminal delivery', [
            ('worker', 'sql', 'Validate exact command and terminal job', 'observe', 'E03'),
            ('worker', 'redis', 'Persist technical snapshot with TTL', 'data', 'E03'),
            ('redis', 'worker', 'If write fails → leave unacked; retry gate', 'branch', 'E03'),
            ('worker', 'rabbit', 'After snapshot succeeds · consumer ACK', 'async', 'E03'),
        ]),
        ('Independent operator requests after eligible evidence exists', [
            ('reviewer', 'api', 'Submit Approval for evaluation and environment', 'manual', 'E06'),
            ('api', 'sql', 'Check matching policy pass; save Approval', 'data', 'E06'),
            ('api', 'reviewer', '201 · Approval', 'return', 'E06'),
            ('deployer', 'api', 'Submit separate Deployment request', 'manual', 'E06'),
            ('api', 'sql', 'Check approval; create REQUESTED generation', 'data', 'E06'),
            ('api', 'deployer', '202 · deployment_id and generation', 'return', 'E06'),
        ]),
        ('Control reconciles the requested generation on its next poll', [
            ('control', 'sql', 'Poll deployment records for reconciliation', 'observe', 'E06'),
            ('control', 'serving', 'Launch fixed generation under exclusive intent', 'launch', 'E06'),
            ('control', 'serving', 'GET /health/ready and verify generation identity', 'sync', 'E06'),
            ('control', 'sql', 'When ready, activate only at expected route revision', 'data', 'E06'),
        ]),
    ],
    'notes': [
        'Relay bookkeeping and worker delivery can interleave after publication. Terminal redelivery reuses the ACK gate.',
        'Approval and deployment require separate permissions. Neither depends on a broker ACK.',
        'Control retries pending readiness. A stale route revision prevents the generation from replacing the current route.',
    ],
}


INFERENCE = {
    'id': '05-inference-sequence',
    'kind': 'sequence',
    'title': 'Prediction against an active deployment',
    'subtitle': 'One request, from tenant-scoped routing to the final SQL check. Guarded exits end the affected request.',
    'participants': [
        ('client', 'Client', 'HTTP caller'),
        ('gateway', 'Gateway', 'ingress process'),
        ('mongo', 'MongoDB', 'identity state'),
        ('sql', 'SQL', 'route + spec'),
        ('serving', 'Serving', 'fixed deployment'),
        ('model', 'Model runtime', 'Serving threads'),
    ],
    'phases': [
        ('Authenticate and resolve the destination', [
            ('client', 'gateway', 'POST /api/v1/prediction/predict + bearer', 'sync', 'E07'),
            ('gateway', 'mongo', 'Read current session and user permissions', 'sync', 'E07'),
            ('gateway', 'sql', 'After ml:predict check, read tenant / endpoint route', 'observe', 'E07'),
            ('gateway', 'client', 'If no active deployment → 503', 'branch', 'E07'),
            ('gateway', 'sql', 'Fetch deployment for route.deployment_id', 'sync', 'E07'),
            ('gateway', 'gateway', 'Check generation and release state; resolve address', 'sync', 'E07'),
        ]),
        ('Probe the chosen Serving instance', [
            ('gateway', 'serving', 'GET /health/ready', 'sync', 'E07'),
            ('serving', 'gateway', 'Readiness, tenant, deployment identity and Pod UID', 'return', 'E07'),
            ('gateway', 'client', 'If unready or identity differs → 503', 'branch', 'E07'),
        ]),
        ('Execute the fixed model', [
            ('gateway', 'serving', 'Forward prediction, bearer and trace context', 'sync', 'E07'),
            ('serving', 'mongo', 'Revalidate current session and user', 'sync', 'E07'),
            ('serving', 'model', 'Predict after scope, endpoint and input checks', 'sync', 'E07'),
            ('model', 'serving', 'Prediction values for the batch', 'return', 'E07'),
            ('serving', 'gateway', 'Predictions with immutable deployment provenance', 'return', 'E07'),
        ]),
        ('Verify before returning', [
            ('gateway', 'gateway', 'For 2xx, match provenance and readiness Pod UID', 'sync', 'E07'),
            ('gateway', 'client', 'If success identity differs → 503', 'branch', 'E07'),
            ('gateway', 'sql', 'Read the same tenant / endpoint route again', 'observe', 'E07'),
            ('sql', 'gateway', 'Current route including revision and generation', 'return', 'E07'),
            ('gateway', 'client', 'If route changed or response redirects → 503', 'branch', 'E07'),
            ('gateway', 'client', 'Stable route · return Serving body and status', 'return', 'E07'),
        ]),
    ],
    'notes': [
        'The Gateway resolver reads the local instance record or derives Kubernetes Service DNS from deployment identity.',
        'Serving loads the pinned bundle at startup. Its request timeout leaves the slot occupied until physical work completes.',
    ],
}


def views():
    return [TRAINING, INFERENCE]
