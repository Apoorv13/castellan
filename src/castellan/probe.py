"""Probe heads: per-class logistic heads on standardized activations.

Pipeline for one layer:
1. ``fit_standardizer``: streaming mean and std over training tokens.
2. ``fit_heads``: one-vs-rest logistic heads trained with mini-batch Adam on
   shuffled batches read from the disk cache. Positives are up-weighted
   (capped) because almost every token is unlabeled.
3. ``fit_platt``: Platt scaling per class on a validation split, which also
   undoes the bias from positive up-weighting.
4. ``choose_thresholds``: per-class hysteresis thresholds on validation. The
   start threshold targets span recall; the continue threshold is set from
   the false positive rate on unlabeled tokens. Both have a floor of 0.05 so
   a weak probe cannot flood spans.

Only numpy is needed, so probes can be applied without torch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

THRESHOLD_FLOOR = 0.05


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * z))


@dataclass
class LinearProbe:
    labels: list[str]
    mean: np.ndarray  # (H,)
    scale: np.ndarray  # (H,)
    W: np.ndarray  # (H, C)
    b: np.ndarray  # (C,)
    platt_a: np.ndarray = field(default=None)  # type: ignore[assignment]
    platt_b: np.ndarray = field(default=None)  # type: ignore[assignment]
    hi: np.ndarray = field(default=None)  # type: ignore[assignment]
    lo: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        C = len(self.labels)
        if self.platt_a is None:
            self.platt_a = np.ones(C)
        if self.platt_b is None:
            self.platt_b = np.zeros(C)
        if self.hi is None:
            self.hi = np.full(C, 0.5)
        if self.lo is None:
            self.lo = np.full(C, 0.25)

    def logits(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mean) / self.scale) @ self.W + self.b

    def proba(self, X: np.ndarray) -> np.ndarray:
        """Calibrated per-class probabilities, shape (N, C)."""
        return sigmoid(self.platt_a * self.logits(X) + self.platt_b)

    def to_arrays(self) -> dict[str, np.ndarray]:
        return {
            "mean": self.mean, "scale": self.scale, "W": self.W, "b": self.b,
            "platt_a": self.platt_a, "platt_b": self.platt_b, "hi": self.hi, "lo": self.lo,
        }

    @classmethod
    def from_arrays(cls, labels: list[str], arr) -> "LinearProbe":
        return cls(labels=list(labels), **{k: np.asarray(arr[k]) for k in
                   ("mean", "scale", "W", "b", "platt_a", "platt_b", "hi", "lo")})


def fit_standardizer(cache, idx: np.ndarray, max_tokens: int = 200_000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    if len(idx) > max_tokens:
        idx = rng.choice(idx, max_tokens, replace=False)
    n = 0
    s = np.zeros(cache.hidden, np.float64)
    ss = np.zeros(cache.hidden, np.float64)
    for X, _ in cache.iter_sequential(idx):
        n += len(X)
        s += X.sum(0)
        ss += (X.astype(np.float64) ** 2).sum(0)
    mean = s / max(n, 1)
    var = np.maximum(ss / max(n, 1) - mean**2, 0.0)
    scale = np.sqrt(var) + 1e-6
    return mean.astype(np.float32), scale.astype(np.float32)


def targets(labels: np.ndarray, class_ids: list[int]) -> np.ndarray:
    return (labels[:, None] == np.asarray(class_ids)[None, :]).astype(np.float32)


def fit_heads(
    cache,
    idx: np.ndarray,
    class_ids: list[int],
    mean: np.ndarray,
    scale: np.ndarray,
    epochs: int = 4,
    batch_size: int = 1024,
    lr: float = 3e-3,
    l2: float = 1e-4,
    max_pos_weight: float = 50.0,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Mini-batch Adam on weighted binary cross-entropy, one head per class."""
    rng = np.random.default_rng(seed)
    H, C = cache.hidden, len(class_ids)
    Y_all = targets(cache.labels[idx], class_ids)
    n_pos = Y_all.sum(0)
    n_neg = len(idx) - n_pos
    pos_w = np.minimum(n_neg / np.maximum(n_pos, 1), max_pos_weight).astype(np.float32)
    prior = np.clip(n_pos / max(len(idx), 1), 1e-6, 1 - 1e-6)

    W = np.zeros((H, C), np.float32)
    b = np.log(prior / (1 - prior)).astype(np.float32)
    mW, vW = np.zeros_like(W), np.zeros_like(W)
    mb, vb = np.zeros_like(b), np.zeros_like(b)
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    t = 0
    for _ in range(epochs):
        for X, tok_idx in cache.iter_batches(idx, batch_size, rng):
            Xs = (X - mean) / scale
            Y = targets(cache.labels[tok_idx], class_ids)
            P = sigmoid(Xs @ W + b)
            w = np.where(Y > 0, pos_w, 1.0)
            G = w * (P - Y) / len(X)
            gW = Xs.T @ G + l2 * W
            gb = G.sum(0)
            t += 1
            mW = beta1 * mW + (1 - beta1) * gW
            vW = beta2 * vW + (1 - beta2) * gW**2
            mb = beta1 * mb + (1 - beta1) * gb
            vb = beta2 * vb + (1 - beta2) * gb**2
            c1, c2 = 1 - beta1**t, 1 - beta2**t
            W -= lr * (mW / c1) / (np.sqrt(vW / c2) + eps)
            b -= lr * (mb / c1) / (np.sqrt(vb / c2) + eps)
    return W, b


def collect_logits(cache, idx: np.ndarray, probe: LinearProbe) -> np.ndarray:
    out = np.zeros((len(idx), len(probe.labels)), np.float32)
    order = np.argsort(idx)
    sorted_idx = idx[order]
    pos = 0
    for X, _ in cache.iter_sequential(sorted_idx):
        out[order[pos : pos + len(X)]] = probe.logits(X)
        pos += len(X)
    return out


def fit_platt(z: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """1-D logistic fit p = sigmoid(a z + b). Falls back to identity without both classes."""
    from sklearn.linear_model import LogisticRegression

    if y.min() == y.max():
        return 1.0, 0.0
    lr = LogisticRegression(C=1e4, max_iter=1000).fit(z.reshape(-1, 1), y.astype(int))
    return float(lr.coef_[0, 0]), float(lr.intercept_[0])


def span_peaks(p: np.ndarray, labels: np.ndarray, docs: np.ndarray, class_id: int) -> np.ndarray:
    """Max score inside each gold span, where a span is a run of adjacent tokens
    in one document with this class label. Inputs are aligned and in token order."""
    is_c = labels == class_id
    if not is_c.any():
        return np.zeros(0)
    starts = is_c & ~np.concatenate([[False], is_c[:-1] & (docs[1:] == docs[:-1])])
    run_id = np.cumsum(starts) - 1
    peaks = np.full(int(run_id[is_c].max()) + 1, -np.inf)
    np.maximum.at(peaks, run_id[is_c], p[is_c])
    return peaks


def choose_thresholds(
    p: np.ndarray,
    labels: np.ndarray,
    docs: np.ndarray,
    class_id: int,
    target_recall: float,
    neg_fpr: float = 0.005,
    max_hi: float = 0.5,
    floor: float = THRESHOLD_FLOOR,
) -> tuple[float, float]:
    """Hysteresis thresholds for one class.

    ``hi`` (start a span) targets span recall: the (1 - target_recall)
    quantile of each gold span's peak score, capped at ``max_hi``. Token-level
    recall is the wrong target, because separator tokens inside a value are
    labeled positive but carry little signal and would drag ``hi`` down until
    benign text floods.

    ``lo`` (continue a span) is the score that only ``neg_fpr`` of unlabeled
    tokens exceed, kept between ``floor`` and ``hi``. Low-scoring separators
    inside a value are handled by bridging in spans.py.
    """
    peaks = span_peaks(p, labels, docs, class_id)
    if len(peaks) == 0:
        return 1.0, 1.0
    hi = float(np.quantile(peaks, 1.0 - target_recall))
    hi = min(max(hi, floor), max_hi)
    neg = p[labels == 0]
    lo = float(np.quantile(neg, 1.0 - neg_fpr)) if len(neg) else floor
    lo = min(max(lo, floor), hi)
    return hi, lo
