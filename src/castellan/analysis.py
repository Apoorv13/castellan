"""Ladder analysis on top of the frozen metrics in evaluate.py.

evaluate.py is frozen, so reporting that the ladder needs lives here:
- Item-level detection. A secret item counts as hit when any gold piece
  overlaps a finding in the secret category (``hit_any``). ``hit_all``
  requires every piece to be hit, which matters for rungs 1 and 5.
- Negative items (hard negatives and matched controls) count as flagged
  when they have any finding in the category. That is the per-rung FPR.
- Rung tables by kind and by announced/unannounced, and per-class tables of
  item recall and char recall (rung 4 must be read per class or by item,
  because spelled-out PEM keys dominate its characters).
- Bootstrap CIs that resample whole bases (all rungs of a base together), with
  matched controls resampled alongside their secret base.

Nothing here reads files or logs values. Inputs are Examples and DocPredictions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np

from castellan.data.schema import SECRET_LABELS, Example
from castellan.evaluate import DocPrediction


@dataclass
class Item:
    id: str
    kind: str
    rung: int | None
    label: str | None  # base label for ladder items
    family: str | None
    announced: bool | None
    base_key: str | None  # matched-pair key: secret base and its control share it
    has_gold: bool
    hit_any: bool
    hit_all: bool
    flagged: bool
    gold_chars: int
    hit_chars: int


def base_key(base_id: str | None) -> str | None:
    """Pair key: the trailing number of a base id, so 'ladneu-007' and 'ladneuctl-007' match."""
    if not base_id:
        return None
    tail = base_id.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else base_id


def items(
    examples: Sequence[Example], preds: Sequence[DocPrediction], labels: Sequence[str] = SECRET_LABELS
) -> list[Item]:
    by_id = {p.id: p for p in preds}
    labs = set(labels)
    out = []
    for ex in examples:
        p = by_id[ex.id]
        gold = [s for s in ex.spans if s.label in labs]
        found = [f for f in p.findings if f.label in labs]
        hits = [any(f.start < g.end and f.end > g.start for f in found) for g in gold]
        mask = np.zeros(len(ex.text), dtype=bool)
        for f in found:
            mask[f.start : f.end] = True
        gc = sum(g.end - g.start for g in gold)
        hc = sum(int(mask[g.start : g.end].sum()) for g in gold)
        m = ex.meta or {}
        out.append(Item(
            id=ex.id, kind=m.get("kind", "unknown"), rung=ex.rung,
            label=m.get("base_label") or (gold[0].label if gold else None),
            family=m.get("family"), announced=m.get("announced"), base_key=base_key(m.get("base_id")),
            has_gold=bool(gold), hit_any=bool(hits) and any(hits), hit_all=bool(hits) and all(hits),
            flagged=bool(found), gold_chars=gc, hit_chars=hc,
        ))
    return out


def _rate(xs: Sequence[bool]) -> float | None:
    return float(np.mean(xs)) if len(xs) else None


def summarize(its: Sequence[Item]) -> dict[str, Any]:
    pos = [i for i in its if i.has_gold]
    neg = [i for i in its if not i.has_gold]
    gc = sum(i.gold_chars for i in pos)
    return {
        "n_pos": len(pos),
        "n_neg": len(neg),
        "item_recall_any": _rate([i.hit_any for i in pos]),
        "item_recall_all": _rate([i.hit_all for i in pos]),
        "pos_flag_rate": _rate([i.flagged for i in pos]),
        "neg_flag_rate": _rate([i.flagged for i in neg]),
        "char_recall": (sum(i.hit_chars for i in pos) / gc) if gc else None,
        "char_recall_item_mean": _rate([i.hit_chars / i.gold_chars for i in pos if i.gold_chars]),
    }


def ladder_tables(its: Sequence[Item]) -> dict[str, Any]:
    """Per rung by kind, per rung by announced, and per rung by class."""
    by_rung: dict[str, dict[str, Any]] = defaultdict(dict)
    by_announced: dict[str, dict[str, Any]] = defaultdict(dict)
    by_class: dict[str, dict[str, Any]] = defaultdict(dict)
    rungs = sorted({i.rung for i in its if i.rung is not None})
    kinds = sorted({i.kind for i in its})
    for r in rungs:
        at = [i for i in its if i.rung == r]
        for k in kinds:
            sel = [i for i in at if i.kind == k]
            if sel:
                by_rung[str(r)][k] = summarize(sel)
        for ann in (True, False):
            sel = [i for i in at if i.announced is ann]
            if sel:
                by_announced[str(r)]["announced" if ann else "unannounced"] = {
                    k: summarize([i for i in sel if i.kind == k]) for k in kinds if any(i.kind == k for i in sel)
                }
        for lab in sorted({i.label for i in at if i.has_gold and i.label}):
            by_class[str(r)][lab] = summarize([i for i in at if i.has_gold and i.label == lab])
    return {"by_rung": dict(by_rung), "by_announced": dict(by_announced), "by_class": dict(by_class)}


def bootstrap_ci(
    its: Sequence[Item],
    stat: Callable[[Sequence[Item]], float | None],
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Point estimate and percentile CI, resampling whole bases (by ``base_key``) with replacement."""
    groups: dict[str, list[Item]] = defaultdict(list)
    for i in its:
        groups[i.base_key or i.id].append(i)
    keys = sorted(groups)
    point = stat(its)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        sample = [it for j in pick for it in groups[keys[j]]]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    if not vals or point is None:
        return {"point": point, "lo": None, "hi": None, "n_bases": len(keys), "n_boot": len(vals)}
    lo, hi = np.quantile(vals, [alpha / 2, 1 - alpha / 2])
    return {"point": point, "lo": float(lo), "hi": float(hi), "n_bases": len(keys), "n_boot": len(vals)}


def neutral_tables(its: Sequence[Item], n_boot: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Per rung: secret item recall and control flag rate, each with a base-resampled 95% CI."""
    out: dict[str, Any] = {}
    for r in sorted({i.rung for i in its if i.rung is not None}):
        at = [i for i in its if i.rung == r]
        out[str(r)] = {
            "secret_item_recall_any": bootstrap_ci(
                at, lambda s: _rate([i.hit_any for i in s if i.kind == "secret"]), n_boot, seed
            ),
            "control_flag_rate": bootstrap_ci(
                at, lambda s: _rate([i.flagged for i in s if i.kind == "control"]), n_boot, seed
            ),
        }
    return out
