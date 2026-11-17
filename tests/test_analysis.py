from __future__ import annotations

import pytest

from castellan.analysis import base_key, bootstrap_ci, items, ladder_tables, neutral_tables, summarize
from castellan.data.schema import Example, Span
from castellan.evaluate import DocPrediction
from castellan.spans import Finding


def _ex(i, kind, rung, spans, ann=None, label="SECRET_JWT", base=None):
    meta = {"kind": kind, "base_label": label, "base_id": base or f"lad-{i:03d}"}
    if ann is not None:
        meta["announced"] = ann
    return Example(f"e{i}-{rung}-{kind}", "x" * 40, spans, rung=rung, source="eval:ladder", meta=meta)


def test_base_key_pairs_secret_and_control():
    assert base_key("ladneu-007") == base_key("ladneuctl-007") == "007"
    assert base_key(None) is None


def test_item_hits_any_all_and_chars():
    ex = _ex(0, "secret", 1, [Span(0, 10, "SECRET_JWT"), Span(20, 30, "SECRET_JWT")])
    p = DocPrediction(ex.id, [Finding(5, 12, "SECRET_GENERIC", 0.9), Finding(35, 38, "EMAIL", 0.9)])
    (it,) = items([ex], [p])
    assert it.hit_any and not it.hit_all and it.flagged
    assert (it.gold_chars, it.hit_chars) == (20, 5)
    # a PII finding alone does not flag the secret category
    neg = _ex(1, "control", 2, [])
    (it2,) = items([neg], [DocPrediction(neg.id, [Finding(0, 3, "EMAIL", 0.9)])])
    assert not it2.flagged


def test_tables_and_item_weighting():
    exs = [
        _ex(0, "secret", 4, [Span(0, 40, "SECRET_PRIVATE_KEY")], ann=True, label="SECRET_PRIVATE_KEY"),
        _ex(1, "secret", 4, [Span(0, 4, "SECRET_JWT")], ann=False),
        _ex(2, "control", 4, [], ann=True),
    ]
    preds = [
        DocPrediction(exs[0].id, [Finding(0, 40, "SECRET_PRIVATE_KEY", 1.0)]),
        DocPrediction(exs[1].id, []),
        DocPrediction(exs[2].id, [Finding(0, 2, "SECRET_GENERIC", 1.0)]),
    ]
    t = ladder_tables(items(exs, preds))
    s = t["by_rung"]["4"]["secret"]
    assert s["item_recall_any"] == 0.5
    assert s["char_recall"] == pytest.approx(40 / 44)  # dominated by the long item
    assert s["char_recall_item_mean"] == pytest.approx(0.5)  # item weighted
    assert t["by_rung"]["4"]["control"]["neg_flag_rate"] == 1.0
    assert t["by_class"]["4"]["SECRET_JWT"]["item_recall_any"] == 0.0
    assert t["by_announced"]["4"]["unannounced"]["secret"]["n_pos"] == 1


def test_bootstrap_resamples_whole_bases():
    # base 000: both secret rungs hit; base 001: both missed. Items within a base move together,
    # so every resample has an even count of hits and the rate is one of 0, 0.5, 1.
    exs, preds = [], []
    for b, hit in (("000", True), ("001", False)):
        for r in (0, 3):
            e = _ex(int(b), "secret", r, [Span(0, 4, "SECRET_JWT")], base=f"ladneu-{b}")
            exs.append(e)
            preds.append(DocPrediction(e.id, [Finding(0, 4, "SECRET_JWT", 1.0)] if hit else []))
    its = items(exs, preds)
    ci = bootstrap_ci(its, lambda s: sum(i.hit_any for i in s) / len(s), n_boot=500)
    assert ci["point"] == 0.5 and ci["n_bases"] == 2
    assert ci["lo"] == 0.0 and ci["hi"] == 1.0
    nt = neutral_tables(its, n_boot=200)
    assert set(nt) == {"0", "3"} and nt["0"]["secret_item_recall_any"]["point"] == 0.5


def test_summarize_empty():
    assert summarize([])["item_recall_any"] is None
