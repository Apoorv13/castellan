from __future__ import annotations

import numpy as np
import pytest

from castellan.data.schema import Example, Span
from castellan.evaluate import (
    DocPrediction,
    auroc,
    compare_operating_points,
    evaluate,
    f_beta,
    fpr_at_recall,
    recall_at_fpr,
    timed_predict,
)
from castellan.spans import Finding


def test_auroc_matches_sklearn():
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 200)
    s = rng.random(200).round(1)  # with ties
    assert auroc(y, s) == pytest.approx(roc_auc_score(y, s))
    assert auroc(np.ones(3), np.ones(3)) is None


def test_recall_at_fpr():
    y = np.array([1] * 10 + [0] * 100)
    s = np.concatenate([np.linspace(0.5, 1.0, 10), np.linspace(0.0, 0.6, 100)])
    r, t = recall_at_fpr(y, s, 0.01)
    # with 1 FP allowed (1% of 100), threshold can drop to the top negative (0.6)
    assert t is not None and r == pytest.approx((s[:10] >= t).mean())
    # binary detector that fires on 5% of negatives gets 0 recall at 1% FPR
    yb = np.array([1] * 10 + [0] * 100)
    sb = np.array([1.0] * 10 + [1.0] * 5 + [0.0] * 95)
    assert recall_at_fpr(yb, sb, 0.01)[0] == 0.0


def test_f2():
    assert f_beta(1.0, 0.5) == pytest.approx(5 * 0.5 / (4 + 0.5))
    assert f_beta(None, 1.0) is None


def _exs():
    return [
        Example("a", "key AAAA BBBB end", [Span(4, 13, "SECRET_CLOUD_KEY")], carrier="env"),
        Example("b", "mail x@y.z now", [Span(5, 10, "EMAIL")], carrier="prose"),
        Example("c", "nothing here at all", [], carrier="prose"),
        Example("d", "tok QQQQ x", [Span(4, 8, "SECRET_JWT")], carrier="env", rung=2),
    ]


def test_evaluate_report():
    preds = [
        DocPrediction("a", [Finding(4, 13, "SECRET_GENERIC", 0.9)]),  # right category, wrong class
        DocPrediction("b", [Finding(5, 8, "EMAIL", 0.8)]),  # partial overlap
        DocPrediction("c", [Finding(0, 7, "SECRET_GENERIC", 0.4)]),  # false positive
        DocPrediction("d", []),  # miss
    ]
    r = evaluate(_exs(), preds)
    sec = r["categories"]["secret"]
    assert sec["span_recall"] == pytest.approx(0.5)
    assert sec["span_precision"] == pytest.approx(0.5)
    assert sec["exact_recall"] == pytest.approx(0.5)
    assert sec["char_recall"] == pytest.approx(9 / 13)
    assert sec["char_precision"] == pytest.approx(9 / 16)
    assert sec["n_pos_docs"] == 2 and sec["doc_auroc"] is not None
    pii = r["categories"]["pattern_pii"]
    assert pii["span_recall"] == 1.0 and pii["exact_recall"] == 0.0
    # class-level: SECRET_GENERIC does not count for SECRET_CLOUD_KEY
    assert r["per_class"]["SECRET_CLOUD_KEY"]["span_recall"] == 0.0
    assert set(r["per_rung"]) == {"none", "2"}
    assert r["per_rung"]["2"]["secret"]["span_recall"] == 0.0
    assert "env" in r["per_carrier"]


def test_evaluate_requires_all_predictions():
    with pytest.raises(ValueError):
        evaluate(_exs(), [])


def test_timed_predict():
    t = timed_predict(lambda exs: [DocPrediction(e.id, []) for e in exs], _exs())
    assert len(t.preds) == 4 and t.ms_per_1k_tokens >= 0 and t.peak_rss_bytes > 0


def test_category_score_prefers_doc_scores_over_findings():
    """Design decision 5: the probe's doc score (rolling mean) must not be replaced by a finding's peak."""
    p = DocPrediction("x", [Finding(0, 3, "SECRET_JWT", 0.99)], {"SECRET_JWT": 0.4, "EMAIL": 0.1})
    assert p.category_score("secret") == pytest.approx(0.4)
    # baselines supply no doc scores: fall back to findings
    assert DocPrediction("y", [Finding(0, 3, "SECRET_JWT", 0.7)]).category_score("secret") == pytest.approx(0.7)
    assert DocPrediction("z", []).category_score("secret") == 0.0


def test_char_fpr_defined_without_gold():
    exs = [Example("n", "x" * 100, [], carrier="prose")]
    r = evaluate(exs, [DocPrediction("n", [Finding(10, 20, "SECRET_GENERIC", 0.9)])])
    sec = r["categories"]["secret"]
    assert sec["char_fpr"] == pytest.approx(0.1)
    assert sec["char_precision"] == 0.0
    assert r["any_label"]["char_fpr"] == pytest.approx(0.1)
    assert r["any_label"]["neg_doc_flag_rate"] == 1.0


def test_doc_flag_rates():
    preds = [
        DocPrediction("a", [Finding(4, 13, "SECRET_GENERIC", 0.9)]),
        DocPrediction("b", []),
        DocPrediction("c", [Finding(0, 7, "PERSON", 0.4)]),  # flags a negative doc, non-secret label
        DocPrediction("d", []),
    ]
    r = evaluate(_exs(), preds)
    # any label: docs a and c flagged out of 4; only c has no gold span at all
    assert r["any_label"]["doc_flag_rate"] == pytest.approx(0.5)
    assert r["any_label"]["neg_doc_flag_rate"] == pytest.approx(1.0)
    sec = r["categories"]["secret"]
    # secret-negative docs are b and c; neither has a secret prediction
    assert sec["neg_doc_flag_rate"] == 0.0 and sec["n_neg_docs"] == 2
    assert sec["doc_flag_rate"] == pytest.approx(0.25)


def test_fpr_at_recall():
    y = np.array([1, 1, 1, 1, 0, 0, 0, 0, 0, 0])
    s = np.array([0.9, 0.8, 0.7, 0.2, 0.75, 0.3, 0.1, 0.05, 0.0, 0.0])
    # recall 0.75 needs t <= 0.7, which flags the 0.75 negative: FPR 1/6
    f, t = fpr_at_recall(y, s, 0.75)
    assert f == pytest.approx(1 / 6) and t == pytest.approx(0.7)
    # recall 1.0 needs t <= 0.2, which flags 0.75 and 0.3: FPR 2/6
    assert fpr_at_recall(y, s, 1.0)[0] == pytest.approx(2 / 6)
    assert fpr_at_recall(y, s, 0.0) == (0.0, None)
    assert fpr_at_recall(np.ones(3), np.ones(3), 0.5) == (None, None)


def _op_examples():
    exs = []
    for i in range(10):
        has = i < 4
        exs.append(Example(f"o{i}", "x" * 20, [Span(0, 5, "SECRET_GENERIC")] if has else [], rung=0 if i % 2 else 2))
    return exs


def test_compare_operating_points():
    exs = _op_examples()
    probe_scores = [0.9, 0.8, 0.7, 0.2, 0.75, 0.3, 0.1, 0.05, 0.0, 0.0]
    probe = [DocPrediction(e.id, [], {"SECRET_GENERIC": sc}) for e, sc in zip(exs, probe_scores)]
    # baseline flags positives o0, o1 and negative o5: recall 0.5, FPR 1/6
    flags = {"o0", "o1", "o5"}
    base = [DocPrediction(e.id, [Finding(0, 5, "SECRET_GENERIC", 1.0)] if e.id in flags else []) for e in exs]
    r = compare_operating_points(exs, probe, base)
    c = r["categories"]["secret"]
    assert c["baseline_recall"] == pytest.approx(0.5) and c["baseline_fpr"] == pytest.approx(1 / 6)
    # probe with FPR <= 1/6: threshold 0.7 flags 3 positives and one negative
    assert c["probe_recall_at_baseline_fpr"] == pytest.approx(0.75)
    # probe with recall >= 0.5: threshold 0.8 flags no negatives
    assert c["probe_fpr_at_baseline_recall"] == 0.0
    assert set(r["per_rung"]) == {"0", "2"}
    # pattern PII has no positives: recall side is undefined, FPR side is trivially 0
    pii = r["categories"]["pattern_pii"]
    assert pii["probe_recall_at_baseline_fpr"] is None and pii["baseline_recall"] is None


def test_compare_requires_all_predictions():
    exs = _op_examples()
    with pytest.raises(ValueError):
        compare_operating_points(exs, [], [])
