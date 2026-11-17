"""End-to-end correctness test: the full pipeline must recover a planted direction.

It was written in M0 against the intended API and has passed since M1.

What it checks:
1. train() on tiny synthetic data finds the planted signal (high val AUROC).
2. scan() on held-out examples returns spans that match the gold spans.
3. Whitespace and punctuation bridging: spans whose interior contains
   unplanted separator tokens come back as ONE span, not fragments.
4. Benign text does not produce spans.
5. The bundle round-trips and refuses a different model revision.
"""

from __future__ import annotations

import pytest

from _tiny import make_examples

pytest.importorskip("torch")
pytest.importorskip("transformers")


def test_pipeline_recovers_planted_signal(tiny_model, tmp_path):
    from castellan.bundle import RevisionMismatch, load_bundle
    from castellan.train import TrainConfig, train

    train_set = make_examples(300, seed=10)
    test_set = make_examples(60, seed=11)

    cfg = TrainConfig(
        model_id=str(tiny_model.path),
        revision="local",
        device="cpu",
        dtype="fp32",
        cache_dir=tmp_path / "cache",
        seed=0,
    )
    result = train(train_set, cfg)
    # Token AUROC is capped below 1: separator tokens inside spans are labeled
    # positive but deliberately carry no planted signal. Span checks below are
    # the real test.
    assert result.val_auroc["SECRET_GENERIC"] > 0.9

    bundle_path = result.bundle.save(tmp_path / "bundle")
    bundle = load_bundle(bundle_path, model_id=str(tiny_model.path), revision="local")

    exact = total = fragmented = false_pos = 0
    for ex in test_set:
        found = [f for f in bundle.scan(ex.text) if f.label == "SECRET_GENERIC"]
        gold = [s for s in ex.spans if s.label == "SECRET_GENERIC"]
        total += len(gold)
        false_pos += sum(1 for f in found if not any(f.start < g.end and f.end > g.start for g in gold))
        for g in gold:
            overlapping = [f for f in found if f.start < g.end and f.end > g.start]
            if len(overlapping) > 1:
                fragmented += 1
            if any(f.start == g.start and f.end == g.end for f in overlapping):
                exact += 1
        # findings carry offsets and scores, never the value
        for f in found:
            assert not hasattr(f, "value") or f.value is None

    assert fragmented == 0, "multi-token spans were split at separator tokens"
    assert exact / max(total, 1) > 0.9
    assert false_pos <= 2, "benign text is flooding spans"

    with pytest.raises(RevisionMismatch):
        load_bundle(bundle_path, model_id=str(tiny_model.path), revision="other")
