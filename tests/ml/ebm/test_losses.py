import numpy as np
import pytest

from src.plugins.training.ebm_losses_v1 import (
    cdf_anchor_loss,
    lot_mean_correlation_loss,
    wafer_count_rank_loss,
)
from src.plugins.training.grouped_rank_sampler import grouped_rank_batches

torch = pytest.importorskip("torch")
pytestmark = pytest.mark.torch


def test_sampler_is_grouped_reproducible_and_drops_short_tail():
    recipes = ["a"] * 7 + ["b"] * 5
    segments = [1] * 4 + [2] * 3 + [2] * 5
    first = grouped_rank_batches(recipes, segments, batch_size=4, rng=np.random.default_rng(9))
    second = grouped_rank_batches(recipes, segments, batch_size=4, rng=np.random.default_rng(9))
    assert len(first) == 3
    assert sorted(len(batch) for batch in first) == [3, 4, 4]
    assert len(np.unique(np.concatenate(first))) == 11
    for actual, repeated in zip(first, second, strict=True):
        np.testing.assert_array_equal(actual, repeated)
        assert len({(recipes[i], segments[i]) for i in actual}) == 1


def test_sampler_can_resume_from_generator_state():
    rng = np.random.default_rng(18)
    recipes, segments = [0] * 20, [0] * 20
    grouped_rank_batches(recipes, segments, batch_size=5, rng=rng)
    state = rng.bit_generator.state
    expected = grouped_rank_batches(recipes, segments, batch_size=5, rng=rng)
    recovered = np.random.default_rng()
    recovered.bit_generator.state = state
    actual = grouped_rank_batches(recipes, segments, batch_size=5, rng=recovered)
    for batch, restored in zip(expected, actual, strict=True):
        np.testing.assert_array_equal(batch, restored)


@pytest.mark.parametrize(
    "recipes,segments,batch_size",
    [([], [], 4), ([0, 0], [0], 4), ([0, 1], [0, 0], 4), ([0] * 4, [0] * 4, 2)],
)
def test_invalid_or_too_small_groups_fail(recipes, segments, batch_size):
    with pytest.raises(ValueError):
        grouped_rank_batches(recipes, segments, batch_size=batch_size, rng=np.random.default_rng(1))


def test_cdf_anchor_matches_hand_calculated_smooth_l1():
    features = torch.tensor([[0.0, 2.0], [-1.0, 1.0]], requires_grad=True)
    loss = cdf_anchor_loss(features, torch.tensor([0.5, 0.5]))
    assert loss.item() == pytest.approx((0 + 1.5 + 0.5 + 0.5) / 4)
    loss.backward()
    assert torch.isfinite(features.grad).all()
    assert features.grad.abs().sum() > 0


def test_cdf_anchor_clamps_endpoints():
    features = torch.zeros(2, 3, requires_grad=True)
    loss = cdf_anchor_loss(features, torch.tensor([0.0, 1.0]))
    assert torch.isfinite(loss)
    loss.backward()
    assert torch.isfinite(features.grad).all()


def test_correlation_uses_lot_means_and_feature_direction():
    cdf = torch.tensor([0.1, 0.3, 0.4, 0.8])
    lots = torch.tensor([0, 0, 1, 2])
    positive = cdf[:, None].repeat(1, 3).requires_grad_()
    assert lot_mean_correlation_loss(positive, cdf, lots).item() == pytest.approx(0, abs=1e-6)
    assert lot_mean_correlation_loss(-positive, cdf, lots).item() == pytest.approx(2, abs=1e-6)


@pytest.mark.parametrize("lots,cdf", [([0, 0, 1], [0.1, 0.5, 0.9]), ([0, 1, 2], [0.5] * 3)])
def test_correlation_without_usable_lots_has_connected_zero(lots, cdf):
    features = torch.ones(3, 4, requires_grad=True)
    loss = lot_mean_correlation_loss(features, torch.tensor(cdf), torch.tensor(lots))
    assert loss.item() == 0
    loss.backward()
    assert torch.equal(features.grad, torch.zeros_like(features))


def rank_loss(features, *, counts=None, lots=None, recipes=None, segments=None, seed=17):
    n = len(features)
    return wafer_count_rank_loss(
        features,
        torch.arange(1, n + 1, dtype=torch.float32)[:, None].repeat(1, 4)
        if counts is None else counts,
        torch.arange(n) if lots is None else lots,
        torch.zeros(n, dtype=torch.int64) if recipes is None else recipes,
        torch.zeros(n, dtype=torch.int64) if segments is None else segments,
        generator=torch.Generator().manual_seed(seed),
    )


def test_count_rank_rewards_correct_direction_and_penalizes_reversal():
    features = torch.arange(3, dtype=torch.float32)[:, None].repeat(1, 4).requires_grad_()
    assert rank_loss(features).item() == 0
    reversed_loss = rank_loss(-features)
    assert reversed_loss.item() > 1
    reversed_loss.backward()
    assert torch.isfinite(features.grad).all()
    assert features.grad.abs().sum() > 0


@pytest.mark.parametrize("boundary", ["lots", "recipes", "segments", "counts"])
def test_count_rank_excludes_invalid_comparisons(boundary):
    features = torch.randn(3, 4, requires_grad=True)
    alternatives = {
        "lots": torch.zeros(3, dtype=torch.int64),
        "recipes": torch.arange(3),
        "segments": torch.arange(3),
        "counts": torch.ones(3, 4),
    }
    loss = rank_loss(features, **{boundary: alternatives[boundary]})
    assert loss.item() == 0
    loss.backward()
    assert torch.equal(features.grad, torch.zeros_like(features))


def test_count_rank_does_not_order_mixed_direction_pad_counts():
    features = torch.zeros(2, 3, requires_grad=True)
    counts = torch.tensor([[1.0, 2.0, 1.0, 2.0], [2.0, 1.0, 2.0, 1.0]])
    assert rank_loss(features, counts=counts).item() == 0


def test_losses_reject_invalid_targets_or_group_metadata():
    features = torch.zeros(3, 4)
    with pytest.raises(ValueError, match="cdf"):
        cdf_anchor_loss(features, torch.tensor([0.1, float("nan"), 0.9]))
    with pytest.raises(ValueError, match="lot_codes"):
        lot_mean_correlation_loss(features, torch.ones(3) / 2, torch.ones(3))
    with pytest.raises(ValueError, match="counts"):
        rank_loss(features, counts=torch.zeros(3, 3))
