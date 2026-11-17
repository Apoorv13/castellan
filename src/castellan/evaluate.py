"""Evaluation metrics. This file is frozen in M2 (see FROZEN.sha256).

Every detector, the probe and every baseline, is scored the same way: it
produces a ``DocPrediction`` per example (findings plus per-label document
scores) and ``evaluate`` turns those into metrics.

Span matching:
- Category level (the headline numbers): a predicted span matches a gold span
  when they overlap by at least one character and both labels fall in the
  same category (secret, pattern_pii, contextual_pii). Baselines use coarser
  label schemes than ours, so category level is the fair comparison.
- Class level: the labels must be equal.
- Exact-boundary recall: gold spans hit with identical start and end by a
  prediction in the same category.

Char level: characters covered by predicted spans against characters covered
by gold spans, per category. This is the redaction-quality measure. We also
report the char false positive rate: predicted characters outside gold spans
over all characters outside gold spans. It stays defined on sets with no gold
spans, such as hard negatives.

Document level: a document is positive for a category when it has a gold span
in it. Its score is the max of the detector's document scores for labels in
that category. We report AUROC and recall at 1% FPR together, never AUROC
alone. Recall at a FPR budget uses the highest recall among thresholds whose
FPR stays within the budget.

Document flag rate: the fraction of documents with at least one predicted
span (``doc_flag_rate``), and the same fraction over documents with no gold
span (``neg_doc_flag_rate``). Reported per category and across all labels
(``any_label``). On a set of benign lookalikes the two are equal. The
``any_label`` block also carries char metrics with labels ignored. The
hard-negative gate reads ``any_label.char_fpr`` and
``any_label.neg_doc_flag_rate`` (see CLAUDE.md section 7).

Breakdowns: per class, per carrier and per ladder rung.

Matched operating points (``compare_operating_points``): a non-scoring
baseline such as gitleaks gives one document-level operating point (a
document is flagged when it has any finding in the category). We report the
probe's recall at the baseline's FPR (the best recall over probe thresholds
whose FPR is at most the baseline's) and the probe's FPR at the baseline's
recall (the lowest FPR over probe thresholds whose recall is at least the
baseline's). The probe side uses its document scores. Reported per category
and per ladder rung.

Findings and metrics never contain matched values.
"""

from __future__ import annotations

import math
import os
import resource
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from castellan.data.schema import (
    CONTEXTUAL_PII_LABELS,
    LABELS,
    PATTERN_PII_LABELS,
    SECRET_LABELS,
    Example,
)
from castellan.spans import Finding

EVALUATE_VERSION = 1

CATEGORIES: dict[str, tuple[str, ...]] = {
    "secret": SECRET_LABELS,
    "pattern_pii": PATTERN_PII_LABELS,
    "contextual_pii": CONTEXTUAL_PII_LABELS,
}
LABEL_CATEGORY = {lab: cat for cat, labs in CATEGORIES.items() for lab in labs}


@dataclass
class DocPrediction:
    id: str
    findings: list[Finding]
    doc_scores: dict[str, float] = field(default_factory=dict)  # label -> score

    def category_score(self, cat: str) -> float:
        """Max of the detector's document scores for labels in this category.

        Finding scores are used only when the detector supplies no document
        scores for the category (the baselines). The probe's document score
        is the max of rolling means, and must not be replaced by its plain
        max token score when a span fires.
        """
        labs = CATEGORIES[cat]
        vals = [v for k, v in self.doc_scores.items() if k in labs]
        if not vals:
            vals = [f.score for f in self.findings if f.label in labs]
        return max(vals) if vals else 0.0


# basic metric helpers


def _safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def f_beta(p: float | None, r: float | None, beta: float = 2.0) -> float | None:
    if p is None or r is None:
        return None
    if p == 0 and r == 0:
        return 0.0
    b2 = beta * beta
    return (1 + b2) * p * r / (b2 * p + r)


def auroc(y: np.ndarray, s: np.ndarray) -> float | None:
    y = np.asarray(y).astype(bool)
    s = np.asarray(s, dtype=float)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    # rank-based (Mann-Whitney) AUROC with average ranks for ties
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    sorted_s = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def recall_at_fpr(y: np.ndarray, s: np.ndarray, max_fpr: float = 0.01) -> tuple[float | None, float | None]:
    """Best recall with FPR <= max_fpr over thresholds 'score >= t'. Returns (recall, threshold)."""
    y = np.asarray(y).astype(bool)
    s = np.asarray(s, dtype=float)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0:
        return None, None
    best_r, best_t = 0.0, math.inf
    for t in np.unique(s)[::-1]:
        pred = s >= t
        fpr = (pred & ~y).sum() / n_neg if n_neg else 0.0
        if fpr > max_fpr:
            break
        r = (pred & y).sum() / n_pos
        if r >= best_r:
            best_r, best_t = float(r), float(t)
    return best_r, (best_t if best_t != math.inf else None)


def fpr_at_recall(y: np.ndarray, s: np.ndarray, min_recall: float) -> tuple[float | None, float | None]:
    """Lowest FPR over thresholds 'score >= t' with recall >= min_recall. Returns (fpr, threshold).

    A ``min_recall`` of 0 is met by flagging nothing, which gives FPR 0 and threshold None.
    """
    y = np.asarray(y).astype(bool)
    s = np.asarray(s, dtype=float)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_neg == 0:
        return None, None
    if min_recall <= 0:
        return 0.0, None
    if n_pos == 0:
        return None, None
    for t in np.unique(s)[::-1]:
        pred = s >= t
        if (pred & y).sum() / n_pos >= min_recall - 1e-12:
            return float((pred & ~y).sum() / n_neg), float(t)
    return None, None


# span and char counting


@dataclass
class Counts:
    gold: int = 0
    gold_hit: int = 0
    gold_exact: int = 0
    pred: int = 0
    pred_hit: int = 0
    gold_chars: int = 0
    pred_chars: int = 0
    overlap_chars: int = 0
    text_chars: int = 0

    def add(self, o: "Counts") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))

    def metrics(self) -> dict[str, Any]:
        p = _safe_div(self.pred_hit, self.pred)
        r = _safe_div(self.gold_hit, self.gold)
        return {
            "span_precision": p,
            "span_recall": r,
            "span_f2": f_beta(p, r, 2.0),
            "exact_recall": _safe_div(self.gold_exact, self.gold),
            "char_precision": _safe_div(self.overlap_chars, self.pred_chars),
            "char_recall": _safe_div(self.overlap_chars, self.gold_chars),
            "char_fpr": _safe_div(self.pred_chars - self.overlap_chars, self.text_chars - self.gold_chars),
            "n_gold": self.gold,
            "n_pred": self.pred,
            "n_pred_chars": self.pred_chars,
        }


def _mask(n: int, spans: Iterable[tuple[int, int]]) -> np.ndarray:
    m = np.zeros(n, dtype=bool)
    for a, b in spans:
        m[max(a, 0) : min(b, n)] = True
    return m


def count_doc(
    text_len: int,
    gold: Sequence[tuple[int, int, str]],
    pred: Sequence[tuple[int, int, str]],
    same: Callable[[str, str], bool],
) -> Counts:
    c = Counts(gold=len(gold), pred=len(pred))
    for ga, gb, gl in gold:
        hits = [(pa, pb) for pa, pb, pl in pred if same(gl, pl) and pa < gb and pb > ga]
        if hits:
            c.gold_hit += 1
        if (ga, gb) in hits:
            c.gold_exact += 1
    for pa, pb, pl in pred:
        if any(same(gl, pl) and pa < gb and pb > ga for ga, gb, gl in gold):
            c.pred_hit += 1
    gm = _mask(text_len, [(a, b) for a, b, _ in gold])
    pm = _mask(text_len, [(a, b) for a, b, _ in pred])
    c.gold_chars, c.pred_chars, c.overlap_chars = int(gm.sum()), int(pm.sum()), int((gm & pm).sum())
    c.text_chars = int(text_len)
    return c


def _cat_same(a: str, b: str) -> bool:
    return LABEL_CATEGORY.get(a) == LABEL_CATEGORY.get(b)


def _label_same(a: str, b: str) -> bool:
    return a == b


def evaluate(
    examples: Sequence[Example],
    preds: Sequence[DocPrediction],
    max_fpr: float = 0.01,
    categories: Sequence[str] = tuple(CATEGORIES),
) -> dict[str, Any]:
    """Full metric report as a JSON-serializable dict."""
    by_id = {p.id: p for p in preds}
    missing = [ex.id for ex in examples if ex.id not in by_id]
    if missing:
        raise ValueError(f"{len(missing)} examples have no prediction")

    cat_counts = {c: Counts() for c in categories}
    class_counts = {lab: Counts() for lab in LABELS}
    carrier_counts: dict[str, dict[str, Counts]] = defaultdict(lambda: {c: Counts() for c in categories})
    rung_counts: dict[str, dict[str, Counts]] = defaultdict(lambda: {c: Counts() for c in categories})
    doc_y = {c: [] for c in categories}
    doc_s = {c: [] for c in categories}
    rung_doc: dict[str, dict[str, tuple[list, list]]] = defaultdict(lambda: {c: ([], []) for c in categories})
    flagged = {c: [] for c in categories}  # (has_gold, has_pred) per doc
    flagged_any: list[tuple[bool, bool]] = []
    any_counts = Counts()

    for ex in examples:
        p = by_id[ex.id]
        n = len(ex.text)
        rung_key = "none" if ex.rung is None else str(ex.rung)
        for cat in categories:
            labs = CATEGORIES[cat]
            g = [(s.start, s.end, s.label) for s in ex.spans if s.label in labs]
            q = [(f.start, f.end, f.label) for f in p.findings if f.label in labs]
            cc = count_doc(n, g, q, _cat_same)
            cat_counts[cat].add(cc)
            carrier_counts[ex.carrier][cat].add(cc)
            rung_counts[rung_key][cat].add(cc)
            y, s = bool(g), p.category_score(cat)
            doc_y[cat].append(y)
            doc_s[cat].append(s)
            rung_doc[rung_key][cat][0].append(y)
            rung_doc[rung_key][cat][1].append(s)
            flagged[cat].append((y, bool(q)))
        flagged_any.append((bool(ex.spans), bool(p.findings)))
        any_counts.add(count_doc(
            n, [(s.start, s.end, s.label) for s in ex.spans], [(f.start, f.end, f.label) for f in p.findings],
            lambda a, b: True,
        ))
        for lab in LABELS:
            g = [(s.start, s.end, s.label) for s in ex.spans if s.label == lab]
            q = [(f.start, f.end, f.label) for f in p.findings if f.label == lab]
            if g or q:
                class_counts[lab].add(count_doc(n, g, q, _label_same))

    def doc_metrics(y, s) -> dict[str, Any]:
        y, s = np.array(y, dtype=bool), np.array(s, dtype=float)
        r, t = recall_at_fpr(y, s, max_fpr)
        return {
            "doc_auroc": auroc(y, s),
            f"doc_recall_at_{max_fpr:g}_fpr": r,
            "doc_threshold": t,
            "n_docs": int(len(y)),
            "n_pos_docs": int(y.sum()),
        }

    def flag_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
        neg = [f for g, f in pairs if not g]
        return {
            "doc_flag_rate": _safe_div(sum(f for _, f in pairs), len(pairs)),
            "neg_doc_flag_rate": _safe_div(sum(neg), len(neg)),
            "n_neg_docs": len(neg),
        }

    report: dict[str, Any] = {
        "evaluate_version": EVALUATE_VERSION,
        "n_examples": len(examples),
        "any_label": {
            **{k: v for k, v in any_counts.metrics().items() if k.startswith("char_") or k == "n_pred_chars"},
            **flag_metrics(flagged_any),
        },
        "categories": {},
        "per_class": {},
        "per_carrier": {},
        "per_rung": {},
    }
    for cat in categories:
        report["categories"][cat] = {
            **cat_counts[cat].metrics(), **doc_metrics(doc_y[cat], doc_s[cat]), **flag_metrics(flagged[cat])
        }
    for lab, c in class_counts.items():
        if c.gold or c.pred:
            report["per_class"][lab] = c.metrics()
    for carrier, d in sorted(carrier_counts.items()):
        report["per_carrier"][carrier] = {cat: d[cat].metrics() for cat in categories if d[cat].gold or d[cat].pred}
    for rung, d in sorted(rung_counts.items()):
        report["per_rung"][rung] = {
            cat: {**d[cat].metrics(), **doc_metrics(*rung_doc[rung][cat])}
            for cat in categories
            if d[cat].gold or d[cat].pred
        }
    return report


# runtime measurement


def peak_rss_bytes() -> int:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(r if sys.platform == "darwin" else r * 1024)


def mps_allocated_bytes() -> int | None:
    try:
        import torch

        if torch.backends.mps.is_available():
            return int(torch.mps.driver_allocated_memory())
    except Exception:
        return None
    return None


@dataclass
class Timed:
    preds: list[DocPrediction]
    seconds: float
    n_tokens: int
    ms_per_1k_tokens: float
    peak_rss_bytes: int
    mps_driver_bytes: int | None


def timed_predict(
    predict: Callable[[Sequence[Example]], list[DocPrediction]],
    examples: Sequence[Example],
    count_tokens: Callable[[str], int] | None = None,
) -> Timed:
    """Run a detector and record latency per 1k tokens and peak memory.

    Without a tokenizer, tokens are approximated as characters / 4.
    """
    n_tok = sum(count_tokens(ex.text) if count_tokens else max(1, len(ex.text) // 4) for ex in examples)
    t0 = time.perf_counter()
    preds = predict(examples)
    dt = time.perf_counter() - t0
    return Timed(
        preds=preds,
        seconds=dt,
        n_tokens=n_tok,
        ms_per_1k_tokens=1000.0 * dt / max(n_tok / 1000.0, 1e-9),
        peak_rss_bytes=peak_rss_bytes(),
        mps_driver_bytes=mps_allocated_bytes(),
    )


def run_info() -> dict[str, Any]:
    return {"pid": os.getpid(), "python": sys.version.split()[0], "evaluate_version": EVALUATE_VERSION}


# matched operating points against a non-scoring baseline


def _matched(y: np.ndarray, probe_s: np.ndarray, base_flag: np.ndarray) -> dict[str, Any]:
    y = np.asarray(y).astype(bool)
    base_flag = np.asarray(base_flag).astype(bool)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    b_rec = _safe_div(int((base_flag & y).sum()), n_pos)
    b_fpr = _safe_div(int((base_flag & ~y).sum()), n_neg)
    out: dict[str, Any] = {
        "n_docs": int(len(y)),
        "n_pos_docs": n_pos,
        "baseline_recall": b_rec,
        "baseline_fpr": b_fpr,
        "probe_recall_at_baseline_fpr": None,
        "probe_threshold_at_baseline_fpr": None,
        "probe_fpr_at_baseline_recall": None,
        "probe_threshold_at_baseline_recall": None,
    }
    if b_fpr is not None and n_pos:
        r, t = recall_at_fpr(y, probe_s, b_fpr)
        out["probe_recall_at_baseline_fpr"], out["probe_threshold_at_baseline_fpr"] = r, t
    if b_rec is not None and n_neg:
        f, t = fpr_at_recall(y, probe_s, b_rec)
        out["probe_fpr_at_baseline_recall"], out["probe_threshold_at_baseline_recall"] = f, t
    return out


def compare_operating_points(
    examples: Sequence[Example],
    probe_preds: Sequence[DocPrediction],
    baseline_preds: Sequence[DocPrediction],
    categories: Sequence[str] = tuple(CATEGORIES),
) -> dict[str, Any]:
    """Probe at the baseline's document-level operating point, per category and per rung."""
    pp = {p.id: p for p in probe_preds}
    bp = {p.id: p for p in baseline_preds}
    missing = [ex.id for ex in examples if ex.id not in pp or ex.id not in bp]
    if missing:
        raise ValueError(f"{len(missing)} examples lack a probe or baseline prediction")
    report: dict[str, Any] = {"categories": {}, "per_rung": {}}
    rungs = sorted({"none" if ex.rung is None else str(ex.rung) for ex in examples})
    for cat in categories:
        labs = CATEGORIES[cat]
        y = np.array([any(s.label in labs for s in ex.spans) for ex in examples])
        ps = np.array([pp[ex.id].category_score(cat) for ex in examples])
        bf = np.array([any(f.label in labs for f in bp[ex.id].findings) for ex in examples])
        report["categories"][cat] = _matched(y, ps, bf)
        for rung in rungs:
            m = np.array([("none" if ex.rung is None else str(ex.rung)) == rung for ex in examples])
            report["per_rung"].setdefault(rung, {})[cat] = _matched(y[m], ps[m], bf[m])
    return report
