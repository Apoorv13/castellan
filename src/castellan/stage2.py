"""Span-level second stage.

The token probe proposes candidate spans (with a lowered start threshold, so
it does not cap recall). A second classifier then scores each candidate from
pooled activations:
    mean and max over the span tokens,
    mean over up to ``ctx`` tokens before the span,
    mean over up to ``ctx`` tokens after the span.
The right context matters: in a causal model, tokens after a value can see it,
so they can carry "this is a placeholder" or "this is a checksum" signals that
the per-token linear probe never sees (the causal blind spot).

Training uses only training data (synth plus mined real negatives). The token
probe used for proposals is cross-fitted: a probe trained on half A of the
training docs proposes candidates on half B and vice versa, so the second
stage sees the kind of mistakes the probe makes on unseen text. Calibration
and the threshold come from a validation split of training docs.

Findings carry offsets, class and score only. Never values.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from castellan.probe import LinearProbe
from castellan.spans import Finding, decode_class, resolve_overlaps, snap

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    tok_start: int
    tok_end: int  # inclusive
    label: str
    score: float  # token probe peak score


def propose(text: str, offsets, scores: np.ndarray, labels: Sequence[str], hi: np.ndarray, lo: np.ndarray,
            max_gap: int = 3) -> list[Candidate]:
    """Candidate token runs per class, before overlap resolution (stage 2 resolves later)."""
    out = []
    for c, lab in enumerate(labels):
        for s, e, sc in decode_class(text, offsets, scores[:, c], float(hi[c]), float(lo[c]), max_gap):
            out.append(Candidate(s, e, lab, sc))
    return out


def pool_features(acts: np.ndarray, s: int, e: int, ctx: int, peak: float) -> np.ndarray:
    """(4H + 2,) feature vector for tokens s..e inclusive."""
    H = acts.shape[1]
    span = acts[s: e + 1]
    left = acts[max(0, s - ctx): s]
    right = acts[e + 1: e + 1 + ctx]
    z = np.zeros(H, np.float32)
    return np.concatenate([
        span.mean(0), span.max(0),
        left.mean(0) if len(left) else z,
        right.mean(0) if len(right) else z,
        np.array([np.log1p(e - s + 1), peak], np.float32),
    ]).astype(np.float32)


def cand_features(acts: np.ndarray, c: Candidate, ctx: int, labels: Sequence[str] | None) -> np.ndarray:
    """Pooled features, plus a one-hot of the proposed class when ``labels`` is given."""
    f = pool_features(acts, c.tok_start, c.tok_end, ctx, c.score)
    if not labels:
        return f
    oh = np.zeros(len(labels), np.float32)
    oh[list(labels).index(c.label)] = 1.0
    return np.concatenate([f, oh])


def all_features(acts: np.ndarray, acts2: np.ndarray | None, c: Candidate, ctx: int,
                 labels: Sequence[str] | None) -> np.ndarray:
    """Candidate features, plus the same pooling over a second layer when ``acts2`` is given."""
    f = cand_features(acts, c, ctx, labels)
    if acts2 is None:
        return f
    return np.concatenate([f, pool_features(acts2, c.tok_start, c.tok_end, ctx, c.score)[:-2]])


VALUE_DELIMS = frozenset("\"'`=:,()[]{}<>")


def extend_to_value(text: str, a: int, b: int, max_ext: int = 200) -> tuple[int, int]:
    """Grow [a, b) outward to the edges of the value it sits in.

    Generic value delimiters only: quotes, '=', ':', commas, brackets and
    whitespace (including line ends). At most ``max_ext`` chars per side, so a
    long undelimited blob cannot swallow a document.
    """
    lo = max(0, a - max_ext)
    while a > lo and not (text[a - 1] in VALUE_DELIMS or text[a - 1].isspace()):
        a -= 1
    hi = min(len(text), b + max_ext)
    while b < hi and not (text[b] in VALUE_DELIMS or text[b].isspace()):
        b += 1
    return a, b


def merge_same_class(findings: list[Finding]) -> list[Finding]:
    """Merge same-class findings that overlap or touch (union span, max score)."""
    out: list[Finding] = []
    for f in sorted(findings, key=lambda f: (f.label, f.start, f.end)):
        if out and out[-1].label == f.label and f.start <= out[-1].end:
            g = out[-1]
            out[-1] = Finding(g.start, max(g.end, f.end), g.label, max(g.score, f.score))
        else:
            out.append(f)
    return out


@dataclass
class Stage2:
    mean: np.ndarray
    scale: np.ndarray
    w: np.ndarray
    b: float
    platt_a: float
    platt_b: float
    threshold: float
    prop_hi: np.ndarray  # proposal start thresholds per class (token probe label order)
    ctx: int = 32
    meta: dict | None = None
    onehot_labels: list[str] | None = None  # class one-hot feature order, None when unused
    prop_lo_floor: float = 0.0  # proposal continuation threshold is at least this
    class_thresholds: list[float] | None = None  # per-class thresholds in token-probe label order
    layer2: int | None = None  # optional second layer pooled into the features
    extend_values: bool = False  # grow kept spans to full delimited values, merge same-class neighbours
    mlp: dict | None = None  # optional one-hidden-layer ReLU net: W1, b1, W2, b2 (replaces w, b)

    def thr(self, i: int) -> float:
        return self.threshold if self.class_thresholds is None else float(self.class_thresholds[i])

    def prop_lo(self, probe_lo: np.ndarray) -> np.ndarray:
        return np.maximum(np.minimum(probe_lo, self.prop_hi), self.prop_lo_floor)

    def logit(self, F: np.ndarray) -> np.ndarray:
        Z = (F - self.mean) / self.scale
        if self.mlp is None:
            return Z @ self.w + self.b
        h = np.maximum(Z @ self.mlp["W1"] + self.mlp["b1"], 0.0)
        return h @ self.mlp["W2"] + self.mlp["b2"]

    def proba(self, F: np.ndarray) -> np.ndarray:
        z = self.logit(F)
        return 1.0 / (1.0 + np.exp(-(self.platt_a * z + self.platt_b)))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        extra = {} if self.mlp is None else {"mlp_" + k: v for k, v in self.mlp.items()}
        np.savez(path / "stage2.npz", mean=self.mean, scale=self.scale, w=self.w, prop_hi=self.prop_hi, **extra)
        (path / "stage2.json").write_text(json.dumps({
            "b": self.b, "platt_a": self.platt_a, "platt_b": self.platt_b, "threshold": self.threshold,
            "ctx": self.ctx, "onehot_labels": self.onehot_labels, "prop_lo_floor": self.prop_lo_floor,
            "class_thresholds": self.class_thresholds, "layer2": self.layer2,
            "extend_values": self.extend_values, "meta": self.meta or {}}, indent=1,
            default=float))

    @classmethod
    def load(cls, path: str | Path) -> "Stage2":
        path = Path(path)
        cfg = json.loads((path / "stage2.json").read_text())
        with np.load(path / "stage2.npz") as a:
            mlp = {k[4:]: a[k] for k in a.files if k.startswith("mlp_")} or None
            return cls(mean=a["mean"], scale=a["scale"], w=a["w"], prop_hi=a["prop_hi"], b=cfg["b"], mlp=mlp,
                       platt_a=cfg["platt_a"], platt_b=cfg["platt_b"], threshold=cfg["threshold"],
                       ctx=cfg["ctx"], meta=cfg.get("meta"),
                       onehot_labels=cfg.get("onehot_labels"), prop_lo_floor=cfg.get("prop_lo_floor", 0.0),
                       class_thresholds=cfg.get("class_thresholds"), layer2=cfg.get("layer2"),
                       extend_values=cfg.get("extend_values", False))


def decode_two_stage(text: str, offsets, acts: np.ndarray, probe: LinearProbe, st2: Stage2,
                     max_gap: int = 3, acts2: np.ndarray | None = None) -> tuple[list[Finding], np.ndarray]:
    """Findings and per-class doc scores (max stage-2 probability of kept candidates)."""
    scores = probe.proba(acts)
    C = len(probe.labels)
    doc = np.zeros(C)
    if len(scores) == 0:
        return [], doc
    cands = propose(text, offsets, scores, probe.labels, st2.prop_hi, st2.prop_lo(probe.lo), max_gap)
    if not cands:
        return [], doc
    F = np.stack([all_features(acts, acts2, c, st2.ctx, st2.onehot_labels) for c in cands])
    p = st2.proba(F)
    lab_idx = {lab: i for i, lab in enumerate(probe.labels)}
    out = []
    for c, pc in zip(cands, p):
        i = lab_idx[c.label]
        doc[i] = max(doc[i], float(pc))
        if pc >= st2.thr(i):
            a, b = snap(text, offsets[c.tok_start][0], offsets[c.tok_end][1])
            if a < b and st2.extend_values:
                a, b = extend_to_value(text, a, b)
            if a < b:
                out.append(Finding(a, b, c.label, float(pc)))
    if st2.extend_values:
        out = merge_same_class(out)
    return resolve_overlaps(out), doc


class TwoStageDetector:
    """Evaluate a token-probe bundle plus a stage-2 classifier like any detector."""

    name = "probe"

    def __init__(self, bundle, stage2: Stage2) -> None:
        self.bundle = bundle
        self.stage2 = stage2

    def predict(self, examples):
        from castellan.evaluate import DocPrediction

        b = self.bundle
        ex = b.extractor
        out = []
        l2 = self.stage2.layer2
        layers = [b.layer] if l2 is None else sorted({b.layer, l2})
        for e, (tok, acts) in zip(examples, ex.extract_many([x.text for x in examples], layers)):
            f, doc = decode_two_stage(e.text, tok.offsets, acts[b.layer], b.probe, self.stage2, b.max_gap,
                                      None if l2 is None else acts[l2])
            out.append(DocPrediction(e.id, f, {lab: float(doc[c]) for c, lab in enumerate(b.labels)}))
        return out


def detector_factory(bundle, bundle_path: str):
    """For scripts/dev_eval.py --detector castellan.stage2:detector_factory."""
    return TwoStageDetector(bundle, Stage2.load(Path(bundle_path) / "stage2"))
