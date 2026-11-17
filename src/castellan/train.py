"""Training: cache -> layer sweep -> fit -> calibrate -> threshold -> bundle.

1. Split training examples by document into train and validation. Validation
   always comes from training data, never from eval sets.
2. Cache activations for up to ``n_sweep`` evenly spaced layers.
3. Sweep: fit heads per layer on a token subsample, score validation AUROC
   per class, and choose the layer with the best mean AUROC.
4. Refit at the best layer on all training tokens, Platt-calibrate and set
   thresholds on validation, and return a pinned bundle.

This module must never import or read eval_data/.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from castellan.bundle import Bundle
from castellan.cache import LABEL_TO_ID, ActivationCache, dataset_hash
from castellan.data.schema import LABELS, Example
from castellan.extractor import Extractor, evenly_spaced_layers
from castellan.probe import (
    LinearProbe,
    choose_thresholds,
    collect_logits,
    fit_heads,
    fit_platt,
    fit_standardizer,
    sigmoid,
)

log = logging.getLogger(__name__)


@dataclass
class TrainConfig:
    model_id: str
    revision: str | None
    device: str = "mps"
    dtype: str = "bf16"
    cache_dir: Path | str = "cache"
    seed: int = 0
    layers: list[int] | None = None  # None = evenly spaced sweep
    n_sweep: int = 8
    val_frac: float = 0.2
    window: int = 512
    stride: int = 384
    batch_size: int = 8
    epochs: int = 4
    sweep_epochs: int = 2
    sweep_max_tokens: int = 300_000
    lr: float = 3e-3
    l2: float = 1e-4
    probe_batch: int = 1024
    target_recall: float = 0.95  # span-level, on validation
    neg_fpr: float = 0.005  # token-level, sets the continue threshold
    max_gap: int = 3
    min_class_tokens: int = 20


@dataclass
class TrainResult:
    bundle: Bundle
    layer: int
    sweep: dict[int, float]
    val_auroc: dict[str, float]
    data_hash: str
    seconds: float
    meta: dict = field(default_factory=dict)


def split_docs(examples: Sequence[Example], val_frac: float, seed: int) -> np.ndarray:
    """Boolean mask over examples, True = validation. Stable per id and seed."""
    def u(ex_id: str) -> float:
        h = hashlib.sha256(f"{seed}:{ex_id}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2**64

    return np.array([u(ex.id) < val_frac for ex in examples])


def _auroc(y: np.ndarray, z: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    if y.min() == y.max():
        return float("nan")
    return float(roc_auc_score(y, z))


def _fit_probe(lc, train_idx, class_ids, labels, cfg: TrainConfig, epochs: int) -> LinearProbe:
    mean, scale = fit_standardizer(lc, train_idx, seed=cfg.seed)
    W, b = fit_heads(
        lc, train_idx, class_ids, mean, scale, epochs=epochs, batch_size=cfg.probe_batch,
        lr=cfg.lr, l2=cfg.l2, seed=cfg.seed,
    )
    return LinearProbe(labels=labels, mean=mean, scale=scale, W=W, b=b)


def train(examples: Sequence[Example], cfg: TrainConfig, extractor: Extractor | None = None) -> TrainResult:
    t0 = time.time()
    examples = list(examples)
    data_hash = dataset_hash(examples)
    ex = extractor or Extractor(
        cfg.model_id, cfg.revision, device=cfg.device, dtype=cfg.dtype,
        window=cfg.window, stride=cfg.stride, batch_size=cfg.batch_size,
    )
    layers = cfg.layers or evenly_spaced_layers(ex.num_layers, cfg.n_sweep)
    cache = ActivationCache(cfg.cache_dir)
    lcs = cache.build(ex, examples, layers, data_hash)

    val_mask_docs = split_docs(examples, cfg.val_frac, cfg.seed)
    any_lc = lcs[layers[0]]
    tok_val = val_mask_docs[any_lc.docs]
    train_idx = np.flatnonzero(~tok_val)
    val_idx = np.flatnonzero(tok_val)
    if len(val_idx) == 0 or len(train_idx) == 0:
        raise ValueError("train/val split is empty; need more examples")

    counts = {lab: int((any_lc.labels[train_idx] == LABEL_TO_ID[lab]).sum()) for lab in LABELS}
    labels = [lab for lab in LABELS if counts[lab] >= cfg.min_class_tokens]
    if not labels:
        raise ValueError("no class has enough labeled training tokens")
    class_ids = [LABEL_TO_ID[lab] for lab in labels]
    y_val = (any_lc.labels[val_idx][:, None] == np.asarray(class_ids)[None, :]).astype(int)

    rng = np.random.default_rng(cfg.seed)
    sweep_idx = train_idx
    if len(train_idx) > cfg.sweep_max_tokens:
        sweep_idx = np.sort(rng.choice(train_idx, cfg.sweep_max_tokens, replace=False))

    sweep: dict[int, float] = {}
    for k in layers:
        probe = _fit_probe(lcs[k], sweep_idx, class_ids, labels, cfg, cfg.sweep_epochs)
        z = collect_logits(lcs[k], val_idx, probe)
        aucs = [_auroc(y_val[:, c], z[:, c]) for c in range(len(labels))]
        sweep[k] = float(np.nanmean(aucs)) if not all(np.isnan(aucs)) else float("nan")
        log.info("layer %d: mean val AUROC %.4f", k, sweep[k])
    best = max(sweep, key=lambda k: -1.0 if np.isnan(sweep[k]) else sweep[k])

    lc = lcs[best]
    probe = _fit_probe(lc, train_idx, class_ids, labels, cfg, cfg.epochs)
    z = collect_logits(lc, val_idx, probe)
    val_auroc = {}
    for c, lab in enumerate(labels):
        val_auroc[lab] = _auroc(y_val[:, c], z[:, c])
        a, b = fit_platt(z[:, c], y_val[:, c])
        probe.platt_a[c], probe.platt_b[c] = a, b
        p = sigmoid(a * z[:, c] + b)
        probe.hi[c], probe.lo[c] = choose_thresholds(
            p, lc.labels[val_idx], lc.docs[val_idx], class_ids[c], cfg.target_recall, cfg.neg_fpr
        )

    meta = {
        "data_hash": data_hash,
        "seed": cfg.seed,
        "n_examples": len(examples),
        "n_train_tokens": int(len(train_idx)),
        "n_val_tokens": int(len(val_idx)),
        "sweep": {str(k): v for k, v in sweep.items()},
        "val_auroc": val_auroc,
        "class_train_tokens": {lab: counts[lab] for lab in labels},
        "target_recall": cfg.target_recall,
        "neg_fpr": cfg.neg_fpr,
        "thresholds": {lab: [float(probe.hi[c]), float(probe.lo[c])] for c, lab in enumerate(labels)},
        "fp32_fallback": ex.fp32_fallback,
    }
    bundle = Bundle(
        model_id=cfg.model_id, revision=cfg.revision, dtype=ex.dtype, layer=best, probe=probe,
        window=cfg.window, stride=cfg.stride, max_gap=cfg.max_gap, meta=meta, device=cfg.device,
    )
    bundle._extractor = ex
    return TrainResult(bundle, best, sweep, val_auroc, data_hash, time.time() - t0, meta)
