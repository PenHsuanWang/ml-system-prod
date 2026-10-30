"""CPU EBM replay training, epoch-boundary recovery and atomic neural bundles."""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from src.domain.datasets.ebm import EBMDatasetMetadata
from src.domain.ml import MLError, digest
from src.domain.training.ebm import (
    EBM_NOTEBOOK_SHA256,
    EBM_RECIPE_ID,
    EBM_RECIPE_VERSION,
    EBMTrainingConfig,
)
from src.plugins.features.fdc_spectrogram_v1 import (
    FittedSpectrogramNormalizer,
    SpectrogramConfig,
)
from src.plugins.models.ebm_cnn_v1 import EBMModelConfig, build_ebm_cnn
from src.plugins.training.ebm_losses_v1 import (
    cdf_anchor_loss,
    lot_mean_correlation_loss,
    wafer_count_rank_loss,
)
from src.plugins.training.grouped_rank_sampler import grouped_rank_batches
from src.plugins.training.pytorch_trainer import atomic_json


class EBMTrainingDataset(Protocol):
    metadata: EBMDatasetMetadata
    spectrograms: Any
    content_digest: str


def _code(value: str) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and value.strip() == value


class EBMTrainerV1:
    def train(self, config: EBMTrainingConfig, directory: Path,
              progress: Callable[[int, dict[str, float]], None], *, dataset: EBMTrainingDataset,
              code_digest: str, model_config: EBMModelConfig | None = None,
              spectrogram_config: SpectrogramConfig | None = None,
              producer: dict[str, str] | None = None, resume: Path | None = None) -> dict[str, Any]:
        import numpy as np
        import torch

        if not isinstance(config, EBMTrainingConfig) or not _code(code_digest):
            raise MLError("EBM requires a validated training config and trusted code digest")
        metadata = EBMDatasetMetadata.parse(dataset.metadata.to_dict())
        raw = dataset.spectrograms
        if (not isinstance(raw, np.ndarray) or raw.dtype != np.dtype("float32")
                or raw.shape != (len(metadata.samples), 7, 4, 9, 31)
                or not np.isfinite(raw).all()):
            raise MLError("Invalid EBM training snapshot")
        if directory.is_symlink() or (directory / "bundle").exists():
            raise MLError("EBM output directory already contains a published bundle")
        directory.mkdir(parents=True, exist_ok=True)
        if resume is None and (directory / "checkpoint.pt").exists():
            raise MLError("Existing EBM checkpoint requires explicit resume")
        resolved_model = model_config or EBMModelConfig()
        resolved_feature = spectrogram_config or SpectrogramConfig(log1p=metadata.spectrogram_log1p)
        if resolved_feature.log1p != metadata.spectrogram_log1p:
            raise MLError("EBM snapshot and training feature configurations differ")
        # Fit each training Lot once; wafer-row duplication still weights the training loss.
        lot_rows: dict[str, int] = {}
        for index in metadata.train:
            lot_rows.setdefault(metadata.samples[index].lot_id, index)
        first_rows: dict[str, int] = {}
        for index, sample in enumerate(metadata.samples):
            first = first_rows.setdefault(sample.lot_id, index)
            if not np.array_equal(raw[first], raw[index]):
                raise MLError("Repeated EBM Lot rows must share identical spectrogram features")
        normalizer = FittedSpectrogramNormalizer.fit(raw, tuple(lot_rows.values()),
                        signal_order=metadata.signal_order, pad_order=metadata.pad_order)
        normalized = normalizer.transform(raw, signal_order=metadata.signal_order,
                                          pad_order=metadata.pad_order)
        selected = [metadata.samples[index] for index in metadata.train]
        if config.wafer_rank_weight and any(row.pad_counts is None for row in selected):
            raise MLError("Enabled EBM wafer rank loss requires all four Pad counts")
        lot_codes = {name: i for i, name in enumerate(dict.fromkeys(row.lot_id for row in selected))}
        recipe_codes = {name: i for i, name in enumerate(dict.fromkeys(row.recipe_id for row in selected))}
        recipes = np.array([recipe_codes[row.recipe_id] for row in selected], dtype=np.int64)
        segments = np.array([row.segment_id for row in selected], dtype=np.int64)
        lineage = {"dataset_digest": dataset.content_digest,
                   "metadata_digest": digest(metadata.to_dict()),
                   "split_indices_digest": digest({"train": metadata.train,
                       "validation": metadata.validation, "test": metadata.test}),
                   "normalizer_lot_indices_digest": digest(tuple(lot_rows.values()))}
        spec = {"recipe_id": EBM_RECIPE_ID, "recipe_version": EBM_RECIPE_VERSION,
                "notebook_sha256": EBM_NOTEBOOK_SHA256, "semantics": "retrospective_replay",
                "training": config.to_dict(), "model": resolved_model.to_dict(),
                "spectrogram": resolved_feature.to_dict(), "code_digest": code_digest,
                "lineage": lineage, "device": "cpu", "torch_version": torch.__version__,
                "numpy_version": np.__version__, "checkpoint_boundary": "completed_epoch"}
        spec_digest = digest(spec)
        atomic_json(directory / "training_spec.json", spec)
        torch.set_num_threads(1)
        torch.manual_seed(config.seed)
        random.seed(config.seed)
        rng = np.random.default_rng(config.seed)
        pair_generator = torch.Generator(device="cpu").manual_seed(config.seed)
        model = build_ebm_cnn(resolved_model)
        optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate,
                                     weight_decay=config.weight_decay)
        x = torch.from_numpy(normalized[list(metadata.train)])
        cdf = torch.tensor([row.cdf_target for row in selected], dtype=torch.float32)
        lots = torch.tensor([lot_codes[row.lot_id] for row in selected], dtype=torch.long)
        counts = torch.tensor([row.pad_counts or (0., 0., 0., 0.) for row in selected],
                              dtype=torch.float32)
        recipe_tensor, segment_tensor = torch.from_numpy(recipes), torch.from_numpy(segments)
        start, history = 0, []
        if resume is not None:
            if resume.is_symlink() or not resume.is_file() or resume.stat().st_size > 128 * 1024 * 1024:
                raise MLError("Invalid EBM checkpoint")
            saved = torch.load(resume, map_location="cpu", weights_only=True)
            if (saved.get("checkpoint_version") != "ebm-checkpoint-v1"
                    or saved.get("spec_digest") != spec_digest
                    or type(saved.get("epoch")) is not int
                    or not 0 <= saved["epoch"] < config.epochs):
                raise MLError("EBM checkpoint dataset, configuration or runtime mismatch")
            model.load_state_dict(saved["model"], strict=True)
            optimizer.load_state_dict(saved["optimizer"])
            torch.set_rng_state(saved["torch_rng"])
            pair_generator.set_state(saved["pair_rng"])
            rng.bit_generator.state = json.loads(saved["numpy_rng_json"])
            random.setstate(saved["python_rng"])
            start, history = saved["epoch"] + 1, saved["metric_history"]
        metrics_dir = directory / "metrics"
        metrics_dir.mkdir(exist_ok=True)
        for epoch in range(start, config.epochs):
            batches = grouped_rank_batches(recipes, segments, batch_size=config.batch_size,
                        min_group_size=config.rank_group_size, rng=rng)
            losses: list[dict[str, float]] = []
            for batch in batches:
                indices = torch.from_numpy(batch)
                model.train()
                optimizer.zero_grad(set_to_none=True)
                latent, reconstruction = model.autoencode(x[indices])
                anchor = cdf_anchor_loss(latent, cdf[indices], eps=config.cdf_anchor_eps)
                correlation = lot_mean_correlation_loss(latent, cdf[indices], lots[indices])
                rank = wafer_count_rank_loss(latent, counts[indices], lots[indices],
                    recipe_tensor[indices], segment_tensor[indices], generator=pair_generator,
                    margin=config.wafer_rank_margin, pair_sample_size=config.pair_sample_size,
                    rank_group_size=config.rank_group_size) if config.wafer_rank_weight else latent.sum() * 0
                reconstruction_loss = (reconstruction - x[indices]).square().mean()
                loss = (config.cdf_anchor_weight * anchor + config.cdf_mean_corr_weight * correlation
                        + config.wafer_rank_weight * rank
                        + config.reconstruction_weight * reconstruction_loss)
                if not torch.isfinite(loss):
                    raise MLError("Non-finite EBM loss")
                loss.backward()
                if any(parameter.grad is not None and not torch.isfinite(parameter.grad).all()
                       for parameter in model.parameters()):
                    raise MLError("Non-finite EBM gradient")
                optimizer.step()
                losses.append({"loss": float(loss.detach()), "cdf_anchor_loss": float(anchor.detach()),
                    "lot_mean_corr_loss": float(correlation.detach()), "wafer_rank_loss": float(rank.detach()),
                    "reconstruction_loss": float(reconstruction_loss.detach())})
            values = {key: sum(row[key] for row in losses) / len(losses) for key in losses[0]}
            history.append({"step": epoch, **values})
            atomic_json(metrics_dir / f"{epoch:06d}.json", history[-1])
            temporary = directory / "checkpoint.tmp"
            torch.save({"checkpoint_version": "ebm-checkpoint-v1", "spec_digest": spec_digest,
                "model": model.state_dict(), "optimizer": optimizer.state_dict(), "epoch": epoch,
                "torch_rng": torch.get_rng_state(), "pair_rng": pair_generator.get_state(),
                "numpy_rng_json": json.dumps(rng.bit_generator.state), "python_rng": random.getstate(),
                "metric_history": history}, temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.replace(directory / "checkpoint.pt")
            # Progress happens after durable checkpointing, so callback interruption can resume.
            progress(epoch, values)
        bundle = self._export(model, raw, metadata, normalizer, resolved_model, resolved_feature,
                              config, directory, spec_digest, lineage, code_digest, producer or {})
        result = {"bundle_path": str(bundle), "manifest_digest": hashlib.sha256(
                    (bundle / "manifest.json").read_bytes()).hexdigest(),
                  "metrics": history, "dataset_digest": dataset.content_digest,
                  "training_spec_digest": spec_digest, "recipe_id": EBM_RECIPE_ID,
                  "quality_decision": "NOT_EVALUATED_BY_POLICY"}
        atomic_json(directory / "result.json", result)
        return result

    @staticmethod
    def _export(model: Any, raw: Any, metadata: EBMDatasetMetadata,
                normalizer: FittedSpectrogramNormalizer, model_config: EBMModelConfig,
                feature_config: SpectrogramConfig, training: EBMTrainingConfig,
                directory: Path, spec_digest: str, lineage: dict[str, str],
                code_digest: str, producer: dict[str, str]) -> Path:
        import numpy as np
        import torch

        bundle = Path(tempfile.mkdtemp(prefix=".bundle-", dir=directory))
        model.eval()
        torch.save(model.state_dict(), bundle / "weights.pt")
        atomic_json(bundle / "model_config.json", model_config.to_dict())
        atomic_json(bundle / "spectrogram_config.json", feature_config.to_dict())
        atomic_json(bundle / "feature_schema.json", {"version": "ebm-spectrogram-input-v1",
            "shape": [None, 7, 4, 9, 31], "dtype": "float32", "granularity": "lot",
            "signal_order": metadata.signal_order, "pad_order": metadata.pad_order,
            "spectrogram_config_digest": digest(feature_config.to_dict())})
        np.savez(bundle / "normalization.npz", mean=normalizer.mean, std=normalizer.std)
        atomic_json(bundle / "preprocessing.json", {"strategy": "ebm-train-lot-standardizer-v1",
            "fit_split": "train", "state_file": "normalization.npz",
            "fit_dataset_digest": lineage["dataset_digest"],
            "fit_indices_digest": lineage["normalizer_lot_indices_digest"],
            "signal_order": metadata.signal_order, "pad_order": metadata.pad_order})
        atomic_json(bundle / "output_mapping.json", {"version": "ebm-neural-output-v1",
            "outputs": ["mu", "sigma", "latent", "raw_nn_score"],
            "score_kind": "uncalibrated_latent_mean_plus_std", "sigma_kind": "latent_feature_std",
            "risk_z": training.risk_z, "latent_dim": 7 * 4 * model_config.features_per_pad})
        golden_input = raw[list(metadata.validation[:2])]
        normalized = normalizer.transform(golden_input, signal_order=metadata.signal_order,
                                           pad_order=metadata.pad_order)
        with torch.inference_mode():
            mu, sigma, latent = model(torch.from_numpy(normalized))
            score = mu + training.risk_z * sigma
        outputs = [{"mu": float(mu[i]), "sigma": float(sigma[i]), "latent": latent[i].tolist(),
                    "raw_nn_score": float(score[i]),
                    "score_kind": "uncalibrated_latent_mean_plus_std"}
                   for i in range(len(golden_input))]
        np.savez(bundle / "golden_input.npz", spectrograms=golden_input)
        atomic_json(bundle / "golden_io.json", {"input_file": "golden_input.npz",
            "samples": len(outputs), "outputs": outputs, "absolute_tolerance": 1e-5})
        files = [{"path": path.name, "size_bytes": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(bundle.iterdir())]
        atomic_json(bundle / "manifest.json", {"manifest_version": "2", "producer": producer,
            "recipe": {"recipe_id": EBM_RECIPE_ID, "recipe_version": EBM_RECIPE_VERSION,
                       "semantics": "retrospective_replay", "notebook_sha256": EBM_NOTEBOOK_SHA256},
            "lineage": {**lineage, "training_spec_digest": spec_digest},
            "artifact": {"format": "trusted_pytorch_state_dict", "profile": "ebm-neural-bundle-v1",
                         "files": files},
            "model": {"architecture_key": "ebm_cnn_v1", "architecture_version": "1",
                      "constructor_config": model_config.to_dict(), "trusted_code_digest": code_digest},
            "runtime": {"compatibility_id": "ebm-torch-cpu-v1", "device": "cpu",
                        "torch_version": torch.__version__, "numpy_version": np.__version__},
            "contract": {"input_schema_version": "ebm-spectrogram-input-v1",
                         "output_schema_version": "ebm-neural-output-v1"}})
        final = directory / "bundle"
        bundle.rename(final)
        return final
