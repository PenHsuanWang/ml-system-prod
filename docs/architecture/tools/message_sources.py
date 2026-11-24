"""Per-message anchors in the implementation. Adjacent lines supply the enclosing transaction or branch."""

HTTP = 'src/api/ml_http.py'
TRAINING_SQL = 'src/infrastructure/persistence/ml_training.py'
DEPLOYMENT_SQL = 'src/infrastructure/persistence/ml_deployments.py'
OUTBOX = 'src/application/training/outbox.py'
PUBLISHER = 'src/infrastructure/messaging/ml_rabbitmq.py'
SUPERVISOR = 'src/application/training/supervisor.py'
DELIVERY = 'src/application/training/delivery.py'
SERVING_PROCESS = 'src/infrastructure/deployment/local_serving.py'
GATEWAY = 'bootstrap/ml_gateway.py'
SERVING = 'bootstrap/ml_serving.py'

SOURCES = {
    '04-training-sequence': {
        'msg-01': (HTTP, 'idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]'),
        'msg-02': (TRAINING_SQL, 'c.execute(insert(outbox).values(id=command_id, job_id=job_id, attempt_id=attempt_id,'),
        'msg-03': (HTTP, '"status_url": f"/api/v1/training/jobs/{job.job_id}"'),
        'msg-04': (OUTBOX, 'items = await asyncio.to_thread(self.repository.lease, limit)'),
        'msg-05': (PUBLISHER, 'message_id=command.command_id, delivery_mode=aio_pika.DeliveryMode.PERSISTENT'),
        'msg-06': ('src/infrastructure/persistence/ml_outbox.py', 'state="DEAD" if row["tries"] >= 20 else "PENDING"'),
        'msg-07': (PUBLISHER, 'if not isinstance(confirmation, Basic.Ack):'),
        'msg-08': (OUTBOX, 'await asyncio.to_thread(self.repository.published, item)'),
        'msg-09': ('bootstrap/ml_worker.py', '@broker.task(task_name=TASK_NAME, ack_type="manual")'),
        'msg-10': (TRAINING_SQL, 'raise Conflict("Command does not match durable intent")'),
        'msg-11': (SUPERVISOR, 'if claim.already_complete:'),
        'msg-12': (SUPERVISOR, 'self.repository.prepare_execution(claim, canonical_json(asdict(handle)))'),
        'msg-13': ('src/infrastructure/execution/local_process.py', 'process = subprocess.Popen([sys.executable, "-m", "bootstrap.ml_training_runner"'),
        'msg-14': (TRAINING_SQL, 'else "FAILED") if confirmed else "NEEDS_RECONCILIATION"'),
        'msg-15': ('src/plugins/training/pytorch_trainer.py', 'atomic_json(directory/"result.json", result)'),
        'msg-16': ('src/infrastructure/execution/local_process.py', 'path = Path(handle.directory)/"result.json"'),
        'msg-17': ('src/infrastructure/mlflow/tracking.py', 'self.client.log_artifacts(run_id, bundle_path, artifact_path="bundle")'),
        'msg-18': (SUPERVISOR, 'version, artifact_ref = self.tracker.register('),
        'msg-19': (SUPERVISOR, 'binding = self.registry.save_binding(ModelBinding('),
        'msg-20': (SUPERVISOR, 'evaluation = self.evaluator.evaluate(binding, claim.job.spec_json)'),
        'msg-21': (SUPERVISOR, 'workload.evaluator_version, "ERROR", canonical_json(metrics), digest(report)'),
        'msg-22': (SUPERVISOR, 'self.repository.finish(claim, binding.binding_id, evaluation.evaluation_id)'),
        'msg-23': (DELIVERY, 'job = await asyncio.to_thread(repository.validate_delivery, command)'),
        'msg-24': ('src/infrastructure/messaging/training_ack.py', '"ml:ack:" + snapshot.command_id, json.dumps(asdict(snapshot)), ex=self.retention'),
        'msg-25': ('bootstrap/ml_worker.py', '"Acknowledgement pending command=%s", command.command_id'),
        'msg-26': (DELIVERY, 'await ack()'),
        'msg-27': (HTTP, 'return asdict(services.registry.approve(actor, binding_id, body.evaluation_id, body.environment))'),
        'msg-28': ('src/application/model_registry/service.py', 'return self.repository.approve(actor, binding, evaluation, environment)'),
        'msg-29': (HTTP, '@registry_router.post("/bindings/{binding_id}/approvals", status_code=201)'),
        'msg-30': (HTTP, 'value = services.deployments.create('),
        'msg-31': (DEPLOYMENT_SQL, 'binding_id, approval_id, "REQUESTED", revision, canonical_json(spec), digest(spec)'),
        'msg-32': (HTTP, '@deployment_router.post("", status_code=202)'),
        'msg-33': ('bootstrap/ml_cli.py', 'rows = connection.execute(select(deployments).where('),
        'msg-34': (SERVING_PROCESS, 'self._processes[deployment.deployment_id] = subprocess.Popen('),
        'msg-35': (SERVING_PROCESS, 'response = httpx.get(metadata["address"]+"/health/ready", timeout=2)'),
        'msg-36': (DEPLOYMENT_SQL, 'routes.c.revision == deployment.expected_route_revision).values('),
    },
    '05-inference-sequence': {
        'msg-01': (GATEWAY, '@app.post("/api/v1/prediction/predict")'),
        'msg-02': ('src/infrastructure/ml_identity.py', 'session = self.db.identity_sessions.find_one('),
        'msg-03': (GATEWAY, 'route = await asyncio.to_thread(state["components"].deployments.route,'),
        'msg-04': (GATEWAY, 'raise Unavailable("No active model")'),
        'msg-05': (GATEWAY, 'deployment = await asyncio.to_thread(state["components"].deployments.get,'),
        'msg-06': (GATEWAY, 'address = state["resolver"].resolve(deployment)'),
        'msg-07': (GATEWAY, 'health = await state["client"].get(address+"/health/ready")'),
        'msg-08': (SERVING, 'return {"ready": bool(state), "deployment_id": state["spec"]["deployment_id"],'),
        'msg-09': (GATEWAY, 'or not matches_identity(deployment, actual)):'),
        'msg-10': (GATEWAY, 'json=body.model_dump(), headers={"Authorization": authorization, **capture_context()}'),
        'msg-11': (HTTP, 'return await request.app.state.ml_identity.resolve(credentials.credentials)'),
        'msg-12': (SERVING, 'result = asdict(state["prediction"].predict(actor, body.endpoint_id, values))'),
        'msg-13': ('src/infrastructure/serving/bounded_prediction.py', 'result = tuple(Prediction(**item) for item in raw)'),
        'msg-14': (SERVING, 'result["provenance"].update(spec_digest=trusted_digest,'),
        'msg-15': (GATEWAY, 'or value["provenance"].get("pod_uid", "") != actual.get("pod_uid", "")):'),
        'msg-16': (GATEWAY, 'raise Unavailable("Serving response identity mismatch")'),
        'msg-17': (GATEWAY, 'current = await asyncio.to_thread(state["components"].deployments.route,'),
        'msg-18': (DEPLOYMENT_SQL, 'return Route(**dict(row))'),
        'msg-19': (GATEWAY, 'if current != route or response.is_redirect:'),
        'msg-20': (GATEWAY, 'return JSONResponse(value, status_code=response.status_code)'),
    },
}
