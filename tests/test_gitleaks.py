"""Gitleaks wrapper tests. Synthetic tokens only; values are never printed."""

from __future__ import annotations

import random
import shutil
import string

import pytest

from castellan.baselines.gitleaks import GitleaksDetector, locate, rule_to_label
from castellan.data.schema import Example, Span


def _fake_pat(seed: int) -> str:
    r = random.Random(seed)
    return "ghp_" + "".join(r.choice(string.ascii_letters + string.digits) for _ in range(36))


def test_rule_map():
    assert rule_to_label("github-pat") == "SECRET_VCS_TOKEN"
    assert rule_to_label("aws-access-token") == "SECRET_CLOUD_KEY"
    assert rule_to_label("private-key") == "SECRET_PRIVATE_KEY"
    assert rule_to_label("something-new") == "SECRET_GENERIC"


def test_locate_prefers_reported_line():
    text = "a XYZ\nb XYZ\n"
    a, b, dec = locate(text, {"Secret": "XYZ", "StartLine": 2})
    assert (a, b, dec) == (8, 11, False)


def test_locate_column_fallback_handles_multibyte_and_line_quirk():
    text = "first\nhé x ABCD y"
    # gitleaks reports byte columns, 1-based, +1 on lines after the first
    a, b, dec = locate(text, {"Secret": "decoded", "StartLine": 2, "EndLine": 2, "StartColumn": 8, "EndColumn": 11})
    assert dec and text[a:b] == "ABCD"


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_detector_end_to_end():
    pats = [_fake_pat(i) for i in range(3)]
    texts = [
        f"line one\n  export GITHUB_TOKEN={pats[0]}\nend",
        f"héllo wörld token: {pats[1]} x",
        f'{{"args": {{"t": "{pats[2]}"}}}}',
        "nothing to see here",
    ]
    exs = []
    for i, t in enumerate(texts):
        spans = []
        if i < 3:
            s = t.index(pats[i])
            spans = [Span(s, s + len(pats[i]), "SECRET_VCS_TOKEN")]
        exs.append(Example(f"g{i}", t, spans))
    det = GitleaksDetector()
    preds = det.predict(exs)
    for ex, p in zip(exs, preds):
        got = [(f.start, f.end) for f in p.findings]
        want = [(s.start, s.end) for s in ex.spans]
        assert got == want, f"example {ex.id}"


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_decoded_finding_maps_to_encoded_segment():
    """Gitleaks decodes base64 by default; the span must cover the encoded text."""
    import base64

    enc = base64.b64encode(f"token={_fake_pat(9)}".encode()).decode()
    texts = [f"x\ncfg: {enc} end", f"hé {enc}"]
    preds = GitleaksDetector().predict([Example(f"e{i}", t, []) for i, t in enumerate(texts)])
    for t, p in zip(texts, preds):
        s = t.index(enc)
        assert [(f.start, f.end) for f in p.findings] == [(s, s + len(enc))]
