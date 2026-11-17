"""Schema and JSONL IO tests. All values are synthetic placeholders."""

from __future__ import annotations

import pytest

from castellan.data.schema import Example, SchemaError, Span, read_jsonl, write_jsonl

ZWJ = "‍"
CYRILLIC_A = "а"  # looks like Latin "a"


def _ex(**kw) -> Example:
    base = dict(id="x1", text="key=abc123 done", spans=[Span(4, 10, "SECRET_GENERIC")])
    base.update(kw)
    return Example(**base)


def test_roundtrip(tmp_path):
    exs = [
        _ex(),
        _ex(id="x2", rung=3, source="eval:ladder", carrier="env", meta={"family": "generic"}),
        _ex(id="x3", text="no secrets here", spans=[], source="real:ai4privacy"),
    ]
    p = tmp_path / "a.jsonl"
    assert write_jsonl(p, exs) == 3
    back = list(read_jsonl(p))
    assert [e.to_dict() for e in back] == [e.to_dict() for e in exs]


def test_roundtrip_preserves_zero_width_and_homoglyphs(tmp_path):
    secret = f"ab{ZWJ}c{CYRILLIC_A}12"
    text = f"token: {secret} end"
    start = text.index(secret)
    ex = _ex(text=text, spans=[Span(start, start + len(secret), "SECRET_VCS_TOKEN")], rung=3)
    p = tmp_path / "z.jsonl"
    write_jsonl(p, [ex])
    raw = p.read_text(encoding="utf-8")
    assert ZWJ in raw and CYRILLIC_A in raw, "must write raw characters, not escapes"
    (back,) = read_jsonl(p)
    s = back.spans[0]
    assert back.text[s.start : s.end] == secret
    assert len(back.text) == len(text)


def test_spans_are_sorted():
    ex = _ex(text="aa bb cc", spans=[Span(6, 8, "EMAIL"), Span(0, 2, "PHONE")])
    assert [s.start for s in ex.spans] == [0, 6]


@pytest.mark.parametrize(
    "kw",
    [
        dict(spans=[Span(4, 99, "SECRET_GENERIC")]),
        dict(spans=[Span(5, 5, "SECRET_GENERIC")]),
        dict(spans=[Span(-1, 3, "SECRET_GENERIC")]),
        dict(spans=[Span(0, 3, "NOT_A_LABEL")]),
        dict(source="scraped"),
        dict(rung=6),
        dict(id=""),
    ],
)
def test_invalid_examples_rejected(kw):
    with pytest.raises(SchemaError):
        _ex(**kw)


def test_repr_and_errors_never_contain_values():
    ex = _ex()
    assert "abc123" not in repr(ex)
    assert "abc123" not in repr(ex.spans[0])
    with pytest.raises(SchemaError) as ei:
        _ex(spans=[Span(4, 99, "SECRET_GENERIC")])
    assert "abc123" not in str(ei.value)


def test_bad_json_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"id": "a"\n', encoding="utf-8")
    with pytest.raises(SchemaError):
        list(read_jsonl(p))
