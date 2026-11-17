"""Turn per-token scores into character spans.

Per class:
1. Hysteresis: a span starts at a token scoring at least ``hi`` and grows
   while tokens score at least ``lo``.
2. Bridging: whitespace and punctuation tokens inside a value often score
   low (they carry little signal), which used to split card numbers and keys
   into fragments. A run of up to ``max_gap`` such tokens is bridged when the
   token after it scores at least ``lo``. A test guards this.
3. Snapping: trim wrapper characters (quotes, brackets, separators) from the
   edges, then extend outward over word characters so a span never ends in
   the middle of an alphanumeric run.

Findings carry offsets, class and score only. Never the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

LEFT_TRIM = set(" \t\r\n\"'`=:,;([{<")
RIGHT_TRIM = set(" \t\r\n\"'`,;)]}>.")


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    label: str
    score: float
    path: str | None = None  # JSON path, set by the guard for structured input

    def __repr__(self) -> str:
        return f"Finding({self.start}, {self.end}, {self.label}, {self.score:.3f}, path={self.path!r})"

    def to_dict(self) -> dict:
        d = {"start": self.start, "end": self.end, "label": self.label, "score": round(float(self.score), 4)}
        if self.path is not None:
            d["path"] = self.path
        return d


def is_bridgeable(text: str, a: int, b: int) -> bool:
    """True for tokens made only of whitespace and punctuation."""
    if a >= b:
        return True
    return all(not ch.isalnum() for ch in text[a:b])


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def snap(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a] in LEFT_TRIM:
        a += 1
    while b > a and text[b - 1] in RIGHT_TRIM:
        b -= 1
    if a >= b:
        return a, b
    while a > 0 and _is_word(text[a - 1]) and _is_word(text[a]):
        a -= 1
    while b < len(text) and _is_word(text[b]) and _is_word(text[b - 1]):
        b += 1
    return a, b


def decode_class(
    text: str,
    offsets: Sequence[tuple[int, int]],
    scores: np.ndarray,
    hi: float,
    lo: float,
    max_gap: int = 3,
) -> list[tuple[int, int, float]]:
    """Token runs as (first_token, last_token_inclusive, max_score)."""
    n = len(scores)
    runs = []
    i = 0
    while i < n:
        if scores[i] < hi:
            i += 1
            continue
        # walk back over adjacent tokens above lo
        s = i
        while s > 0 and scores[s - 1] >= lo and not is_bridgeable(text, *offsets[s - 1]):
            s -= 1
        e = i
        while True:
            j = e + 1
            if j < n and scores[j] >= lo:
                e = j
                continue
            # try to bridge whitespace and punctuation tokens
            k = j
            while k < n and k - j < max_gap and scores[k] < lo and is_bridgeable(text, *offsets[k]):
                k += 1
            if k < n and k > j and scores[k] >= lo:
                e = k
                continue
            break
        runs.append((s, e, float(scores[s : e + 1].max())))
        i = e + 1
    return runs


def decode_spans(
    text: str,
    offsets: Sequence[tuple[int, int]],
    scores: np.ndarray,
    labels: Sequence[str],
    hi: np.ndarray,
    lo: np.ndarray,
    max_gap: int = 3,
) -> list[Finding]:
    """All classes. ``scores`` is (T, C) calibrated probability."""
    out: list[Finding] = []
    for c, lab in enumerate(labels):
        for s, e, sc in decode_class(text, offsets, scores[:, c], float(hi[c]), float(lo[c]), max_gap):
            a, b = snap(text, offsets[s][0], offsets[e][1])
            if a < b:
                out.append(Finding(a, b, lab, sc))
    return resolve_overlaps(out)


def resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Keep the higher-scoring finding when two classes claim overlapping text."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda f: -f.score):
        if all(f.end <= k.start or f.start >= k.end for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: (f.start, f.end))


def doc_scores(scores: np.ndarray, window: int = 3) -> np.ndarray:
    """Per-class document score: the max over rolling means of token scores."""
    T = len(scores)
    if T == 0:
        return np.zeros(scores.shape[1] if scores.ndim == 2 else 0)
    w = min(window, T)
    c = np.cumsum(np.vstack([np.zeros((1, scores.shape[1])), scores]), axis=0)
    means = (c[w:] - c[:-w]) / w
    return means.max(axis=0)
