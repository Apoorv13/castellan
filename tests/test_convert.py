from __future__ import annotations

from pathlib import Path

import pytest

from castellan.data.convert import ConvertStats, LabelMap, convert_records, load_map

MAPS = Path(__file__).resolve().parents[1] / "maps"


def _map(**kw):
    base = dict(dataset="x/y", text_field="t", spans_field="s", labels={"GIVENNAME": "PERSON", "SURNAME": "PERSON", "TIME": None},
                language_field="lang", languages=["en"])
    base.update(kw)
    return LabelMap(**base)


def test_convert_maps_drops_counts_and_merges():
    text = "hi Ann Lee at 10am ok"
    rec = {"t": text, "lang": "en", "s": [
        {"start": 3, "end": 6, "label": "GIVENNAME"}, {"start": 7, "end": 10, "label": "SURNAME"},
        {"start": 14, "end": 18, "label": "TIME"}, {"start": 19, "end": 21, "label": "WEIRD"}]}
    st = ConvertStats()
    (ex,) = list(convert_records([rec, {"t": "x", "lang": "de", "s": []}], _map(), st))
    assert [(s.start, s.end, s.label) for s in ex.spans] == [(3, 10, "PERSON")]
    assert ex.source == "real:x/y"
    assert st.unmapped["WEIRD"] == 1 and st.dropped["TIME"] == 1 and st.skipped_language == 1


def test_invalid_offsets_skip_record():
    st = ConvertStats()
    out = list(convert_records([{"t": "ab", "lang": "en", "s": [{"start": 0, "end": 9, "label": "SURNAME"}]}], _map(), st))
    assert out == [] and st.skipped_invalid == 1


def test_map_rejects_unknown_target():
    with pytest.raises(ValueError):
        _map(labels={"A": "NOPE"})


def test_shipped_map_loads():
    pytest.importorskip("yaml")
    m = load_map(MAPS / "ai4privacy.yaml")
    assert m.labels["EMAIL"] == "EMAIL"
