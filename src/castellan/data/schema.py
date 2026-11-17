"""JSONL example schema.

One example per line:

    {"id": "...", "text": "...", "spans": [{"start": 10, "end": 42, "label": "SECRET_CLOUD_KEY"}],
     "carrier": "tool_call_json", "source": "synth|real:<dataset>|eval:<generator>",
     "rung": null, "meta": {}}

Offsets are Python code-point indices into ``text``. Text is never normalized
(no NFC or NFKC) on read or write, because rung 3 of the ladder relies on
zero-width characters and homoglyphs, and normalizing would shift offsets.

Reprs and error messages never include text or span values, so that test
failures and logs cannot leak sensitive material, even synthetic material.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

SECRET_LABELS: tuple[str, ...] = (
    "SECRET_CLOUD_KEY",
    "SECRET_AI_KEY",
    "SECRET_VCS_TOKEN",
    "SECRET_JWT",
    "SECRET_PRIVATE_KEY",
    "SECRET_DB_URL",
    "SECRET_GENERIC",
)
PATTERN_PII_LABELS: tuple[str, ...] = ("EMAIL", "PHONE", "CARD", "GOV_ID", "IP_ADDRESS")
CONTEXTUAL_PII_LABELS: tuple[str, ...] = ("PERSON", "ADDRESS", "CONTEXTUAL", "HEALTH")
LABELS: tuple[str, ...] = SECRET_LABELS + PATTERN_PII_LABELS + CONTEXTUAL_PII_LABELS
LABEL_SET = frozenset(LABELS)

MAX_RUNG = 5
_SOURCE_RE = re.compile(r"^(synth|real:[A-Za-z0-9_.\-/]+|eval:[A-Za-z0-9_.\-/]+)$")


class SchemaError(ValueError):
    """Raised when an example does not match the schema. Never carries values."""


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    label: str

    def __repr__(self) -> str:
        return f"Span({self.start}, {self.end}, {self.label})"

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "label": self.label}


@dataclass
class Example:
    id: str
    text: str
    spans: list[Span] = field(default_factory=list)
    carrier: str = "prose"
    source: str = "synth"
    rung: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.spans = sorted(self.spans, key=lambda s: (s.start, s.end))
        validate(self)

    def __repr__(self) -> str:
        return (
            f"Example(id={self.id!r}, len={len(self.text)}, spans={len(self.spans)}, "
            f"carrier={self.carrier!r}, source={self.source!r}, rung={self.rung})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "spans": [s.to_dict() for s in self.spans],
            "carrier": self.carrier,
            "source": self.source,
            "rung": self.rung,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Example":
        missing = {"id", "text", "spans", "carrier", "source"} - d.keys()
        if missing:
            raise SchemaError(f"missing keys: {sorted(missing)}")
        try:
            spans = [Span(int(s["start"]), int(s["end"]), str(s["label"])) for s in d["spans"]]
        except (KeyError, TypeError, ValueError) as e:
            raise SchemaError(f"example {d.get('id')!r}: malformed span ({type(e).__name__})") from None
        return cls(
            id=str(d["id"]),
            text=d["text"],
            spans=spans,
            carrier=str(d["carrier"]),
            source=str(d["source"]),
            rung=d.get("rung"),
            meta=dict(d.get("meta") or {}),
        )


def validate(ex: Example) -> None:
    """Check structural invariants. Messages name the example and span index only."""
    if not isinstance(ex.text, str):
        raise SchemaError(f"example {ex.id!r}: text must be str")
    if not ex.id:
        raise SchemaError("example id must be non-empty")
    if not _SOURCE_RE.match(ex.source):
        raise SchemaError(f"example {ex.id!r}: bad source {ex.source!r}")
    if ex.rung is not None and (not isinstance(ex.rung, int) or not 0 <= ex.rung <= MAX_RUNG):
        raise SchemaError(f"example {ex.id!r}: rung must be null or 0..{MAX_RUNG}")
    n = len(ex.text)
    for i, s in enumerate(ex.spans):
        if s.label not in LABEL_SET:
            raise SchemaError(f"example {ex.id!r}: span {i} has unknown label {s.label!r}")
        if not 0 <= s.start < s.end <= n:
            raise SchemaError(
                f"example {ex.id!r}: span {i} offsets ({s.start}, {s.end}) out of range for len {n}"
            )


def read_jsonl(path: str | Path) -> Iterator[Example]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                raise SchemaError(f"{path}:{lineno}: invalid JSON") from None
            yield Example.from_dict(d)


def write_jsonl(path: str | Path, examples: Iterable[Example]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for ex in examples:
            f.write(json.dumps(ex.to_dict(), ensure_ascii=False, sort_keys=True))
            f.write("\n")
            n += 1
    return n
