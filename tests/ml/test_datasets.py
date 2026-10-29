from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.domain.ml import Conflict, MLError, NotFound, canonical_json
from src.infrastructure.datasets.snapshots import FileDatasetSource
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


def snapshot_value():
    return json.loads(Path('src/infrastructure/datasets/dataset-demo.json').read_text())


def test_dataset_import_is_immutable_and_tenant_scoped(components, actor, tmp_path):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    operator = replace(actor, permissions=actor.permissions | {"ml:dataset:manage"})
    source = tmp_path/'data.json'
    value = snapshot_value()
    source.write_text(canonical_json(value))
    registered = catalog.register(operator, 'sensors', 'v1', source)
    assert catalog.resolve(actor, 'sensors', 'v1').content_digest == registered.content_digest
    assert FileDatasetSource(components.settings.dataset_root).load(
        {'snapshot_ref': registered.snapshot_ref, 'content_digest': registered.content_digest}).train
    with pytest.raises(NotFound):
        catalog.resolve(replace(actor, tenant_id='other'), 'sensors', 'v1')
    value['labels'] = [1-label for label in value['labels']]
    source.write_text(canonical_json(value))
    with pytest.raises(Conflict):
        catalog.register(operator, 'sensors', 'v1', source)
    assert catalog.resolve(actor, 'sensors', 'v1') == registered


def test_overlapping_splits_and_mutated_bytes_are_rejected(components, actor, tmp_path):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    operator = replace(actor, permissions=actor.permissions | {"ml:dataset:manage"})
    value = snapshot_value()
    value['splits']['test'][0] = value['splits']['train'][0]
    source = tmp_path/'data.json'
    source.write_text(canonical_json(value))
    with pytest.raises(MLError):
        catalog.register(operator, 'sensors', 'bad', source)
    source.write_text(canonical_json(snapshot_value()))
    registered = catalog.register(operator, 'sensors', 'v1', source)
    Path(registered.snapshot_ref).write_text('{}')
    with pytest.raises(MLError):
        FileDatasetSource(components.settings.dataset_root).load(
            {'snapshot_ref': registered.snapshot_ref, 'content_digest': registered.content_digest})


@pytest.mark.torch
def test_real_training_uses_registered_data_not_the_demo(components, actor, intent, tmp_path):
    pytest.importorskip('torch')
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    value = snapshot_value()
    value['labels'] = [1-label for label in value['labels']]
    source = tmp_path/'inverted.json'
    source.write_text(canonical_json(value))
    operator = replace(actor, permissions=actor.permissions | {"ml:dataset:manage"})
    registered = catalog.register(operator, 'custom-sensors', 'v1', source)
    request = json.loads(intent)
    request['dataset'] = {'id': 'custom-sensors', 'version': 'v1'}
    job = components.services.training.submit(actor, 'custom', canonical_json(request))
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    components.supervisor().execute(command)
    completed = components.training.get(job.job_id)
    assert completed.state == 'SUCCEEDED'
    binding = components.registry.get_model(actor, completed.model_binding_id)
    evaluation = components.registry.list_evaluations(binding.binding_id)[0]
    assert evaluation.dataset_digest == registered.content_digest
    manifest = json.loads((Path(binding.artifact_ref)/'manifest.json').read_text())
    assert manifest['lineage']['dataset_digest'] == registered.content_digest
    assert manifest['lineage']['training_spec_digest'] == completed.spec_digest
    assert manifest['preprocessing']['fit_split'] == 'train'
    from src.infrastructure.serving.pytorch_runtime import PyTorchRuntime
    from src.plugins.models.mlp import build_mlp
    runtime = PyTorchRuntime(Path(binding.artifact_ref), binding.manifest_digest, build_mlp)
    try:
        assert runtime.predict([[1.0, 1.0]])[0]['class_id'] == 0
    finally:
        runtime.close()
