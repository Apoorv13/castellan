"""A saved probe, pinned to the model id, revision and dtype it was trained on.

Layout of a bundle directory:
    bundle.json   model pin, layer, window settings, labels, training metadata
    probe.npz     probe arrays

``load_bundle`` checks the pin. Asking for a different revision, model or
dtype raises ``BundleMismatch`` (``RevisionMismatch`` for the revision).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from castellan.probe import LinearProbe
from castellan.spans import Finding, decode_spans, doc_scores

BUNDLE_VERSION = 1


class BundleMismatch(Exception):
    pass


class RevisionMismatch(BundleMismatch):
    pass


@dataclass
class ScoredText:
    offsets: list[tuple[int, int]]
    scores: np.ndarray  # (T, C)
    findings: list[Finding]
    doc: np.ndarray  # (C,)


@dataclass
class Bundle:
    model_id: str
    revision: str | None
    dtype: str
    layer: int
    probe: LinearProbe
    window: int = 512
    stride: int = 384
    max_gap: int = 3
    doc_window: int = 3
    meta: dict[str, Any] = field(default_factory=dict)
    device: str = "cpu"
    _extractor: Any = field(default=None, repr=False)

    @property
    def labels(self) -> list[str]:
        return self.probe.labels

    def attach(self, extractor) -> "Bundle":
        """Use an already-loaded extractor (must match the pin)."""
        if extractor.model_id != self.model_id or extractor.revision != self.revision:
            raise BundleMismatch("extractor does not match the bundle's model pin")
        self._extractor = extractor
        return self

    @property
    def extractor(self):
        if self._extractor is None:
            from castellan.extractor import Extractor

            self._extractor = Extractor(
                self.model_id, self.revision, device=self.device, dtype=self.dtype,
                window=self.window, stride=self.stride,
            )
        return self._extractor

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        np.savez(path / "probe.npz", **self.probe.to_arrays())
        cfg = {
            "version": BUNDLE_VERSION,
            "model_id": self.model_id,
            "revision": self.revision,
            "dtype": self.dtype,
            "layer": self.layer,
            "labels": self.probe.labels,
            "window": self.window,
            "stride": self.stride,
            "max_gap": self.max_gap,
            "doc_window": self.doc_window,
            "meta": self.meta,
        }
        (path / "bundle.json").write_text(json.dumps(cfg, indent=1, sort_keys=True))
        return path

    # scoring

    def _decode(self, text: str, offsets, scores: np.ndarray) -> ScoredText:
        p = self.probe
        findings = decode_spans(text, offsets, scores, p.labels, p.hi, p.lo, self.max_gap)
        return ScoredText(offsets, scores, findings, doc_scores(scores, self.doc_window))

    def score(self, text: str) -> ScoredText:
        """Token scores with max-merge over overlapping windows."""
        ex = self.extractor
        tok = ex.tokenize(text)
        C = len(self.labels)
        scores = np.zeros((len(tok), C), np.float32)
        for a, b, acts in ex.iter_windows(tok, [self.layer]):
            scores[a:b] = np.maximum(scores[a:b], self.probe.proba(acts[self.layer]))
        return self._decode(text, tok.offsets, scores)

    def score_many(self, texts: Sequence[str]) -> list[ScoredText]:
        """Batches short texts; long texts fall back to the windowed max-merge path."""
        ex = self.extractor
        out: list[ScoredText | None] = [None] * len(texts)
        short = []
        for i, t in enumerate(texts):
            if len(ex.tokenize(t)) > ex.window:
                out[i] = self.score(t)
            else:
                short.append(i)
        for i, (tok, acts) in zip(short, ex.extract_many([texts[i] for i in short], [self.layer])):
            out[i] = self._decode(texts[i], tok.offsets, self.probe.proba(acts[self.layer]))
        return out  # type: ignore[return-value]

    def scan(self, text: str) -> list[Finding]:
        return self.score(text).findings


def load_bundle(
    path: str | Path,
    model_id: str | None = None,
    revision: str | None = None,
    dtype: str | None = None,
    device: str = "cpu",
) -> Bundle:
    path = Path(path)
    cfg = json.loads((path / "bundle.json").read_text())
    if model_id is not None and model_id != cfg["model_id"]:
        raise BundleMismatch("bundle was trained on a different model id")
    if revision is not None and revision != cfg["revision"]:
        raise RevisionMismatch(f"bundle pinned to revision {cfg['revision']!r}, asked for {revision!r}")
    if dtype is not None and dtype != cfg["dtype"]:
        raise BundleMismatch(f"bundle pinned to dtype {cfg['dtype']!r}, asked for {dtype!r}")
    with np.load(path / "probe.npz") as arr:
        probe = LinearProbe.from_arrays(cfg["labels"], arr)
    return Bundle(
        model_id=cfg["model_id"], revision=cfg["revision"], dtype=cfg["dtype"], layer=cfg["layer"],
        probe=probe, window=cfg["window"], stride=cfg["stride"], max_gap=cfg["max_gap"],
        doc_window=cfg["doc_window"], meta=cfg.get("meta", {}), device=device,
    )


class ProbeDetector:
    """Adapter so a bundle can be evaluated like any baseline."""

    name = "probe"

    def __init__(self, bundle: Bundle) -> None:
        self.bundle = bundle

    def predict(self, examples):
        from castellan.evaluate import DocPrediction

        scored = self.bundle.score_many([ex.text for ex in examples])
        return [
            DocPrediction(ex.id, st.findings, {lab: float(st.doc[c]) for c, lab in enumerate(self.bundle.labels)})
            for ex, st in zip(examples, scored)
        ]
