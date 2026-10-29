import json

import pytest


@pytest.mark.torch
def test_resume_matches_uninterrupted_cpu_training(tmp_path, components, intent):
    torch = pytest.importorskip("torch")
    from src.plugins.training.pytorch_trainer import PyTorchTrainer
    spec = json.loads(components.services.training.recipes.resolve(intent))
    spec["training"]["epochs"] = 4
    interrupted, resumed, reference = (
        tmp_path/name for name in ("interrupted", "resumed", "reference"))
    for path in (interrupted, resumed, reference):
        path.mkdir()

    class Interrupted(Exception):
        pass

    def progress(step, values):
        if step == 2:
            raise Interrupted()
    with pytest.raises(Interrupted):
        PyTorchTrainer().train(spec, interrupted, progress)
    PyTorchTrainer().train(spec, resumed, lambda step, values: None, resume=interrupted/"checkpoint.pt")
    PyTorchTrainer().train(spec, reference, lambda step, values: None)
    actual = torch.load(resumed/"bundle"/"weights.pt", weights_only=True)
    expected = torch.load(reference/"bundle"/"weights.pt", weights_only=True)
    assert actual.keys() == expected.keys()
    for name in actual:
        assert torch.equal(actual[name], expected[name])
