from __future__ import annotations

from dataclasses import dataclass

import pytest

from castellan.baselines.presidio import map_results
from castellan.data.schema import Example, Span


@dataclass
class _R:
    entity_type: str
    start: int
    end: int
    score: float


def test_map_results_keeps_mapped_types_only():
    fs = map_results([_R("EMAIL_ADDRESS", 5, 10, 1.0), _R("DATE_TIME", 0, 3, 0.85), _R("US_SSN", 12, 23, 0.5)])
    assert [(f.label, f.start) for f in fs] == [("EMAIL", 5), ("GOV_ID", 12)]


def test_map_results_trims_whitespace():
    text = "id: AB123456C ok"
    (f,) = map_results([_R("UK_NINO", 3, 14, 0.5)], text)
    assert (f.start, f.end) == (4, 13)


def _has_model() -> bool:
    try:
        import spacy

        return spacy.util.is_package("en_core_web_lg")
    except Exception:
        return False


@pytest.mark.skipif(not _has_model(), reason="en_core_web_lg not installed")
def test_presidio_end_to_end():
    from castellan.baselines.presidio import PresidioDetector

    text = "contact jane.doe@example.com today"
    s = text.index("jane")
    ex = Example("p0", text, [Span(s, s + len("jane.doe@example.com"), "EMAIL")])
    (p,) = PresidioDetector().predict([ex])
    assert any(f.label == "EMAIL" and f.start == s for f in p.findings)


@pytest.mark.skipif(not _has_model(), reason="en_core_web_lg not installed")
def test_uk_nino_enabled_and_mapped():
    from castellan.baselines.presidio import PresidioDetector

    det = PresidioDetector()
    assert any("UK_NINO" in r["entities"] for r in det.recognizer_config())
    assert det.display_name == "Presidio + UK NINO" and det.name == "presidio"
    text = "my national insurance number is AB 12 34 56 C thanks"
    s = text.index("AB")
    (p,) = det.predict([Example("n0", text, [])])
    assert any(f.label == "GOV_ID" and f.start == s for f in p.findings)
