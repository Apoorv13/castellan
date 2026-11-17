"""Unit tests for span decoding, thresholds, probes and bundle pinning."""

from __future__ import annotations

import numpy as np
import pytest

from castellan.bundle import Bundle, BundleMismatch, RevisionMismatch, load_bundle
from castellan.probe import LinearProbe, choose_thresholds, span_peaks
from castellan.spans import Finding, decode_spans, doc_scores, is_bridgeable, snap


def _word_offsets(text: str, pieces: list[str]) -> list[tuple[int, int]]:
    out, pos = [], 0
    for p in pieces:
        i = text.index(p, pos)
        out.append((i, i + len(p)))
        pos = i + len(p)
    return out


def test_whitespace_bridging_regression():
    """Regression: low-scoring whitespace tokens used to split a card number into 4 spans."""
    text = "card 4000 1234 5678 9010 ok"
    pieces = ["card", "4000", " ", "1234", " ", "5678", " ", "9010", "ok"]
    offs = _word_offsets(text, pieces)
    s = np.array([[0.0], [0.9], [0.01], [0.9], [0.01], [0.8], [0.01], [0.9], [0.0]])
    fs = decode_spans(text, offs, s, ["CARD"], np.array([0.5]), np.array([0.3]))
    assert len(fs) == 1
    assert (fs[0].start, fs[0].end) == (5, 24)


def test_punctuation_bridging_and_gap_limit():
    text = "k AB-CD/EF x"
    pieces = ["k", "AB", "-", "CD", "/", "EF", "x"]
    offs = _word_offsets(text, pieces)
    s = np.array([[0], [0.9], [0.0], [0.9], [0.0], [0.9], [0]], dtype=float)
    (f,) = decode_spans(text, offs, s, ["SECRET_GENERIC"], np.array([0.5]), np.array([0.3]))
    assert (f.start, f.end) == (2, 10)
    # an alphanumeric low token is NOT bridged
    s2 = s.copy()
    pieces2 = ["k", "AB", "zz", "CD"]
    text2 = "k AB zz CD"
    offs2 = _word_offsets(text2, pieces2)
    s2 = np.array([[0], [0.9], [0.0], [0.9]], dtype=float)
    assert len(decode_spans(text2, offs2, s2, ["SECRET_GENERIC"], np.array([0.5]), np.array([0.3]))) == 2


def test_hysteresis_needs_hi_to_start():
    text = "aa bb cc"
    offs = [(0, 2), (3, 5), (6, 8)]
    s = np.array([[0.4], [0.4], [0.4]])
    assert decode_spans(text, offs, s, ["EMAIL"], np.array([0.5]), np.array([0.3])) == []
    s[1, 0] = 0.6
    (f,) = decode_spans(text, offs, s, ["EMAIL"], np.array([0.5]), np.array([0.3]))
    assert (f.start, f.end) == (0, 8)


def test_snap_trims_wrappers_and_extends_words():
    text = 'key="abcdef", x'
    a, b = snap(text, 3, 13)  # '="abcdef",'
    assert text[a:b] == "abcdef"
    a, b = snap(text, 7, 9)  # mid-word
    assert text[a:b] == "abcdef"


def test_is_bridgeable():
    assert is_bridgeable(" - ", 0, 3)
    assert not is_bridgeable("a-", 0, 2)


def test_overlap_resolution_prefers_higher_score():
    text = "abc def"
    offs = [(0, 3), (4, 7)]
    s = np.array([[0.9, 0.6], [0.9, 0.6]])
    fs = decode_spans(text, offs, s, ["SECRET_GENERIC", "PERSON"], np.array([0.5, 0.5]), np.array([0.3, 0.3]))
    assert [f.label for f in fs] == ["SECRET_GENERIC"]


def test_doc_score_is_max_rolling_mean():
    s = np.array([[0.0], [1.0], [0.0], [0.0], [0.9], [0.9], [0.9]])
    assert doc_scores(s, 3)[0] == pytest.approx(0.9)
    assert doc_scores(np.zeros((0, 2)), 3).shape == (2,)


def test_finding_has_no_value():
    f = Finding(1, 5, "SECRET_JWT", 0.9)
    assert not hasattr(f, "value")
    assert set(f.to_dict()) == {"start", "end", "label", "score"}


def test_span_peaks_and_thresholds():
    labels = np.array([0, 3, 3, 0, 3, 0, 3], dtype=np.int16)
    docs = np.array([0, 0, 0, 0, 0, 0, 1])
    p = np.array([0.01, 0.9, 0.1, 0.02, 0.7, 0.03, 0.8])
    np.testing.assert_allclose(span_peaks(p, labels, docs, 3), [0.9, 0.7, 0.8])
    hi, lo = choose_thresholds(p, labels, docs, 3, target_recall=1.0)
    assert hi == 0.5  # capped
    assert 0.05 <= lo <= hi
    hi, lo = choose_thresholds(p, labels, docs, 7, target_recall=0.9)
    assert hi == 1.0  # class absent: never fires


def _bundle(tmp_path):
    H, C = 4, 1
    probe = LinearProbe(["SECRET_GENERIC"], np.zeros(H), np.ones(H), np.ones((H, C)), np.zeros(C))
    b = Bundle(model_id="m", revision="r1", dtype="bf16", layer=2, probe=probe)
    return b.save(tmp_path / "b")


def test_bundle_roundtrip_and_pins(tmp_path):
    path = _bundle(tmp_path)
    b = load_bundle(path, model_id="m", revision="r1", dtype="bf16")
    assert b.layer == 2 and b.labels == ["SECRET_GENERIC"]
    np.testing.assert_allclose(b.probe.proba(np.ones((1, 4))), 1 / (1 + np.exp(-4)), rtol=1e-6)
    with pytest.raises(RevisionMismatch):
        load_bundle(path, revision="r2")
    with pytest.raises(BundleMismatch):
        load_bundle(path, model_id="other")
    with pytest.raises(BundleMismatch):
        load_bundle(path, dtype="fp32")
