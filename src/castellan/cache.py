"""Disk-backed activation cache.

Layout under ``root``:

    index.json                         maps entry key -> metadata
    <dataset_key>/tokens.json          per-dataset metadata (doc ids, counts)
    <dataset_key>/labels.i16           (N,) token label ids, 0 = no label
    <dataset_key>/docs.i32             (N,) index of the example each token came from
    <dataset_key>/layer_<k>.bf16       (N, H) activations as raw bfloat16 bits (uint16)

An entry key is a hash of (model id, revision, dtype, layer, dataset hash).
Activations are stored as bfloat16 bits for every model dtype, which halves
disk use against fp32 and converts back to float32 exactly and cheaply.

Training reads shuffled mini-batches straight from the memmaps, so nothing
needs to fit in RAM.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

from castellan.data.schema import LABELS, Example, Span

LABEL_TO_ID = {lab: i + 1 for i, lab in enumerate(LABELS)}


def dataset_hash(examples: Sequence[Example]) -> str:
    h = hashlib.sha256()
    for ex in examples:
        h.update(json.dumps(ex.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def entry_key(model_id: str, revision: str | None, dtype: str, layer: int, data_hash: str) -> str:
    raw = json.dumps([model_id, revision, dtype, int(layer), data_hash])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def dataset_key(model_id: str, revision: str | None, dtype: str, data_hash: str) -> str:
    raw = json.dumps([model_id, revision, dtype, data_hash])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def token_labels(offsets: Sequence[tuple[int, int]], spans: Sequence[Span]) -> np.ndarray:
    """Label id per token: a token takes the label of any span it overlaps."""
    out = np.zeros(len(offsets), dtype=np.int16)
    if not spans:
        return out
    for i, (a, b) in enumerate(offsets):
        if a >= b:
            continue
        for s in spans:
            if a < s.end and b > s.start:
                out[i] = LABEL_TO_ID[s.label]
                break
    return out


def f32_to_bf16_bits(x: np.ndarray) -> np.ndarray:
    """Round-to-nearest-even float32 -> bfloat16 bit pattern."""
    u = np.ascontiguousarray(x, dtype=np.float32).view(np.uint32)
    rounding = ((u >> 16) & 1) + np.uint32(0x7FFF)
    return ((u + rounding) >> 16).astype(np.uint16)


def bf16_bits_to_f32(b: np.ndarray) -> np.ndarray:
    return (b.astype(np.uint32) << 16).view(np.float32)


@dataclass
class LayerCache:
    path: Path
    layer: int
    n_tokens: int
    hidden: int
    labels: np.ndarray  # (N,) int16
    docs: np.ndarray  # (N,) int32

    def _mm(self) -> np.memmap:
        return np.memmap(self.path, dtype=np.uint16, mode="r", shape=(self.n_tokens, self.hidden))

    def read(self, idx: np.ndarray | slice) -> np.ndarray:
        mm = self._mm()
        if isinstance(idx, np.ndarray):
            idx = np.sort(idx)
        return bf16_bits_to_f32(np.asarray(mm[idx]))

    def iter_batches(
        self, idx: np.ndarray, batch_size: int, rng: np.random.Generator
    ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Shuffled (X float32, token index) batches over the given token indices."""
        perm = rng.permutation(idx)
        mm = self._mm()
        for i in range(0, len(perm), batch_size):
            b = np.sort(perm[i : i + batch_size])
            yield bf16_bits_to_f32(np.asarray(mm[b])), b

    def iter_sequential(self, idx: np.ndarray, batch_size: int = 8192) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        idx = np.sort(idx)
        mm = self._mm()
        for i in range(0, len(idx), batch_size):
            b = idx[i : i + batch_size]
            yield bf16_bits_to_f32(np.asarray(mm[b])), b


class ActivationCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    def _index(self) -> dict:
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {}

    def _write_index(self, idx: dict) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=1, sort_keys=True))
        tmp.replace(self.index_path)

    def get(self, model_id: str, revision: str | None, dtype: str, layer: int, data_hash: str) -> LayerCache | None:
        meta = self._index().get(entry_key(model_id, revision, dtype, layer, data_hash))
        if meta is None:
            return None
        d = self.root / meta["dataset_dir"]
        n = meta["n_tokens"]
        return LayerCache(
            path=d / f"layer_{layer}.bf16",
            layer=layer,
            n_tokens=n,
            hidden=meta["hidden"],
            labels=np.fromfile(d / "labels.i16", dtype=np.int16),
            docs=np.fromfile(d / "docs.i32", dtype=np.int32),
        )

    def build(
        self,
        extractor,
        examples: Sequence[Example],
        layers: Sequence[int],
        data_hash: str | None = None,
    ) -> dict[int, LayerCache]:
        """Extract and store activations for every missing layer in one pass."""
        data_hash = data_hash or dataset_hash(examples)
        mid, rev, dt = extractor.model_id, extractor.revision, extractor.dtype
        layers = sorted(set(int(k) for k in layers))
        missing = [k for k in layers if self.get(mid, rev, dt, k, data_hash) is None]
        if missing:
            self._build_layers(extractor, examples, missing, data_hash)
        out = {}
        for k in layers:
            lc = self.get(mid, rev, dt, k, data_hash)
            assert lc is not None
            out[k] = lc
        return out

    def _build_layers(self, extractor, examples, layers, data_hash) -> None:
        mid, rev, dt = extractor.model_id, extractor.revision, extractor.dtype
        dkey = dataset_key(mid, rev, dt, data_hash)
        d = self.root / dkey
        d.mkdir(parents=True, exist_ok=True)

        toks = [extractor.tokenize(ex.text) for ex in examples]
        n = sum(len(t) for t in toks)
        H = extractor.hidden_size
        labels = np.concatenate(
            [token_labels(t.offsets, ex.spans) for t, ex in zip(toks, examples)] or [np.zeros(0, np.int16)]
        ).astype(np.int16)
        docs = np.concatenate(
            [np.full(len(t), i, dtype=np.int32) for i, t in enumerate(toks)] or [np.zeros(0, np.int32)]
        )
        labels.tofile(d / "labels.i16")
        docs.tofile(d / "docs.i32")
        (d / "tokens.json").write_text(
            json.dumps({"ids": [ex.id for ex in examples], "n_tokens": int(n)}, ensure_ascii=False)
        )

        mms = {
            k: np.memmap(d / f"layer_{k}.bf16", dtype=np.uint16, mode="w+", shape=(max(n, 1), H)) for k in layers
        }
        pos = 0
        for tok, acts in extractor.extract_many([ex.text for ex in examples], layers):
            m = len(tok)
            for k in layers:
                mms[k][pos : pos + m] = f32_to_bf16_bits(acts[k])
            pos += m
        assert pos == n, "token count changed between tokenize and extract"
        for mm in mms.values():
            mm.flush()
        del mms

        idx = self._index()
        for k in layers:
            idx[entry_key(mid, rev, dt, k, data_hash)] = {
                "model_id": mid,
                "revision": rev,
                "dtype": dt,
                "layer": k,
                "data_hash": data_hash,
                "dataset_dir": dkey,
                "n_tokens": int(n),
                "hidden": int(H),
            }
        self._write_index(idx)
