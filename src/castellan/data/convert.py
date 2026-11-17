"""Convert third-party span-labeled datasets into our schema with YAML label maps.

A map file looks like:

    dataset: ai4privacy/pii-masking-400k
    text_field: source_text
    spans_field: privacy_mask
    start_key: start
    end_key: end
    label_key: label
    language_field: language      # optional
    languages: [en]               # optional filter
    labels:
      EMAIL: EMAIL
      TIME: null                  # null = drop this source label

Source labels missing from ``labels`` are dropped and counted, so a first run
shows what the map does not cover. Counts are reported, never values.
Adjacent spans with the same target label (for example a given name followed
by a surname, both mapped to PERSON) are merged when only whitespace
separates them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from castellan.data.schema import LABEL_SET, Example, Span


@dataclass
class LabelMap:
    dataset: str
    text_field: str
    spans_field: str
    labels: dict[str, str | None]
    start_key: str = "start"
    end_key: str = "end"
    label_key: str = "label"
    language_field: str | None = None
    languages: list[str] | None = None
    carrier: str = "prose"
    merge_adjacent: bool = True

    def __post_init__(self) -> None:
        bad = {v for v in self.labels.values() if v is not None and v not in LABEL_SET}
        if bad:
            raise ValueError(f"map targets unknown labels: {sorted(bad)}")


def load_map(path: str | Path) -> LabelMap:
    import yaml

    d = yaml.safe_load(Path(path).read_text())
    return LabelMap(**d)


@dataclass
class ConvertStats:
    seen: int = 0
    kept: int = 0
    skipped_language: int = 0
    skipped_invalid: int = 0
    unmapped: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    mapped: Counter = field(default_factory=Counter)


def _merge(text: str, spans: list[Span]) -> list[Span]:
    out: list[Span] = []
    for s in sorted(spans, key=lambda s: (s.start, s.end)):
        if out and out[-1].label == s.label and out[-1].end <= s.start and text[out[-1].end : s.start].strip() == "":
            out[-1] = Span(out[-1].start, s.end, s.label)
        elif out and s.start < out[-1].end:
            continue  # overlapping source spans: keep the first
        else:
            out.append(s)
    return out


def convert_records(records: Iterable[dict[str, Any]], m: LabelMap, stats: ConvertStats | None = None,
                    id_prefix: str | None = None) -> Iterator[Example]:
    stats = stats if stats is not None else ConvertStats()
    prefix = id_prefix or m.dataset.replace("/", "_")
    for i, rec in enumerate(records):
        stats.seen += 1
        if m.languages and m.language_field and rec.get(m.language_field) not in m.languages:
            stats.skipped_language += 1
            continue
        text = rec.get(m.text_field)
        if not isinstance(text, str):
            stats.skipped_invalid += 1
            continue
        spans = []
        ok = True
        for s in rec.get(m.spans_field) or []:
            src = s[m.label_key]
            if src not in m.labels:
                stats.unmapped[src] += 1
                continue
            tgt = m.labels[src]
            if tgt is None:
                stats.dropped[src] += 1
                continue
            a, b = int(s[m.start_key]), int(s[m.end_key])
            if not 0 <= a < b <= len(text):
                ok = False
                break
            spans.append(Span(a, b, tgt))
            stats.mapped[tgt] += 1
        if not ok:
            stats.skipped_invalid += 1
            continue
        if m.merge_adjacent:
            spans = _merge(text, spans)
        stats.kept += 1
        yield Example(
            id=f"{prefix}-{rec.get('id', i)}", text=text, spans=spans, carrier=m.carrier,
            source=f"real:{m.dataset}", meta={},
        )
