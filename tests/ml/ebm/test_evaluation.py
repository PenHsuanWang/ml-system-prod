import json
import math

import pytest

from src.domain.datasets.ebm import EBMDatasetMetadata, EBMSample
from src.plugins.evaluation.ebm_v1 import (
    LOT_CORRELATION_METRIC,
    SCORE_KIND,
    EBMQualityPolicy,
    evaluate_neural_outputs,
)


def metadata(targets, lots=None):
    lot_ids = lots or [f"lot-{index}" for index in range(len(targets))]
    samples = tuple(
        EBMSample(f"sample-{index}", lot_ids[index], "recipe-a", 1, f"wafer-{index}",
                  "2026-01-01T00:00:00Z", None, target)
        for index, target in enumerate(targets)
    )
    return EBMDatasetMetadata(samples, (0,), (1,), tuple(range(2, len(samples))))


def prediction(latent, score=0.0):
    return {"mu": sum(latent) / len(latent), "sigma": 1e-6, "latent": latent,
            "raw_nn_score": score, "score_kind": SCORE_KIND}


def test_anchor_uses_every_latent_coordinate_and_selected_sample_order():
    data = metadata([0.1, 0.2, 0.25, 0.5, 0.75])
    indices = [4, 2, 3]
    logits = [math.log(data.samples[index].cdf_target / (1 - data.samples[index].cdf_target))
              for index in indices]
    outputs = [prediction([logit, logit], logit) for logit in logits]
    metrics = evaluate_neural_outputs(outputs, data, indices)
    assert metrics["cdf_anchor_smooth_l1"] == 0
    assert metrics["samples"] == 3
    assert metrics["lots"] == 3
    assert metrics[LOT_CORRELATION_METRIC] == pytest.approx(1)
    metrics = evaluate_neural_outputs(
        [prediction([-2.0, 0.0, 2.0])], metadata([0.1, 0.2, 0.5]), [2]
    )
    assert metrics["cdf_anchor_smooth_l1"] == 1
    assert metrics[LOT_CORRELATION_METRIC] is None


def test_endpoint_cdf_targets_are_clamped_using_explicit_epsilon():
    data = metadata([0.2, 0.3, 0.0, 1.0])
    for epsilon in (0.01, 0.05):
        logits = [math.log(epsilon / (1 - epsilon)), math.log((1 - epsilon) / epsilon)]
        metrics = evaluate_neural_outputs(
            [prediction([value], value) for value in logits], data, [2, 3],
            cdf_anchor_eps=epsilon,
        )
        assert metrics["cdf_anchor_smooth_l1"] == pytest.approx(0, abs=1e-20)
        assert metrics[LOT_CORRELATION_METRIC] == pytest.approx(1)


def test_correlation_gives_each_lot_one_vote_after_wafer_aggregation():
    data = metadata([0.2, 0.3, 0.0, 0.0, 0.5, 1.0],
                    ["train", "validation", "a", "a", "b", "c"])
    metrics = evaluate_neural_outputs(
        [prediction([0.0], score) for score in (-1, 1, 1, 2)], data, [2, 3, 4, 5]
    )
    assert metrics["lots"] == 3
    assert metrics["samples"] == 4
    # Per-wafer correlation would be less than one for these deliberately unequal scores.
    assert metrics[LOT_CORRELATION_METRIC] == pytest.approx(1)


@pytest.mark.parametrize("targets,scores", [
    ([0.2, 0.2], [1.0, 2.0]),
    ([0.2, 0.8], [1.0, 1.0]),
    ([0.0, 0.0], [0.0, 0.0]),
])
def test_undefined_correlation_is_none_and_serializes_without_nan(targets, scores):
    metrics = evaluate_neural_outputs(
        [prediction([0], score) for score in scores], metadata([0.1, 0.3, *targets]), [2, 3]
    )
    assert metrics[LOT_CORRELATION_METRIC] is None
    json.dumps(metrics, allow_nan=False)


def test_large_finite_outputs_do_not_overflow_aggregation():
    metrics = evaluate_neural_outputs(
        [prediction([-1e300], -1e300), prediction([1e300], 1e300)],
        metadata([0.2, 0.3, 0.1, 0.9]), [2, 3],
    )
    assert math.isfinite(metrics["cdf_anchor_smooth_l1"])
    assert metrics[LOT_CORRELATION_METRIC] == pytest.approx(1)


@pytest.mark.parametrize("indices", [[], [2, 2], [-1], [5], [True], [2.0]])
def test_invalid_sample_selection_is_rejected(indices):
    with pytest.raises(ValueError, match="indices"):
        evaluate_neural_outputs([prediction([0])] * len(indices), metadata([0.1, 0.2, 0.5]), indices)


@pytest.mark.parametrize("key,value", [
    ("mu", float("nan")), ("sigma", 0.0), ("raw_nn_score", float("inf")),
    ("latent", []), ("latent", [float("nan")]), ("latent", [True]),
    ("score_kind", "calibrated_energy"),
])
def test_malformed_predictions_are_rejected(key, value):
    output = prediction([0])
    output[key] = value
    with pytest.raises(ValueError):
        evaluate_neural_outputs([output], metadata([0.1, 0.2, 0.5]), [2])


def test_quality_policy_has_explicit_thresholds_and_no_metric_only_approval():
    # These thresholds exercise policy mechanics; they are not product qualification.
    policy = EBMQualityPolicy("engineering-fixture-v1", 0.5, 0.8, 3)
    metrics = {"cdf_anchor_smooth_l1": 0.5, LOT_CORRELATION_METRIC: 0.8,
               "samples": 6, "lots": 3}
    assert policy.decide(metrics) == "APPROVED_BY_POLICY"
    restored = EBMQualityPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
    assert restored == policy
    for key, bad in (
        ("cdf_anchor_smooth_l1", 0.6), ("cdf_anchor_smooth_l1", -1),
        ("cdf_anchor_smooth_l1", float("nan")), (LOT_CORRELATION_METRIC, 0.7),
        (LOT_CORRELATION_METRIC, None), (LOT_CORRELATION_METRIC, 1.1),
        ("lots", 2), ("lots", True), ("samples", 2),
    ):
        assert policy.decide({**metrics, key: bad}) == "REJECTED_BY_POLICY"
    assert policy.decide({}) == "REJECTED_BY_POLICY"
    with pytest.raises(TypeError):
        EBMQualityPolicy("missing-thresholds")
    with pytest.raises(ValueError, match="complete explicit"):
        EBMQualityPolicy.from_dict({})
    raw_metrics = evaluate_neural_outputs([prediction([0])], metadata([0.1, 0.2, 0.5]), [2])
    assert "decision" not in raw_metrics
    assert "policy_version" not in raw_metrics


@pytest.mark.parametrize("version,loss,correlation,lots", [
    ("", 1.0, 0.0, 2), ("valid", -1, 0.0, 2), ("valid", float("inf"), 0.0, 2),
    ("valid", 1.0, float("nan"), 2), ("valid", 1.0, 1.1, 2), ("valid", 1.0, 0.0, 1),
    ("valid", True, 0.0, 2), ("valid", 1.0, 0.0, True),
])
def test_invalid_quality_policy_is_rejected(version, loss, correlation, lots):
    with pytest.raises(ValueError):
        EBMQualityPolicy(version, loss, correlation, lots)
