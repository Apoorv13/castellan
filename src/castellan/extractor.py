"""Residual-stream capture via forward hooks.

Layer k means the output of ``model.model.layers[k]``. Hooks are registered
only on the layers we need, and the forward pass stops right after the
deepest requested layer, so later layers and the LM head never run.

Long inputs are split into overlapping token windows. ``extract`` keeps, for
each token, the activation from the first window that contains it (that
window gives the token the most left context). Scoring paths that want the
max over overlapping windows use ``iter_windows`` directly.

Token offsets: Qwen3 tokenizers keep a token's leading space in its char
offsets. ``tokenize`` trims leading whitespace so offsets point at content.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterator, Sequence

import numpy as np

log = logging.getLogger(__name__)

DTYPE_NAMES = ("fp32", "bf16", "fp16")


class _StopForward(Exception):
    """Raised from the deepest hook to end the forward pass early."""


@dataclass
class Tokenized:
    input_ids: list[int]
    offsets: list[tuple[int, int]]  # char offsets with leading whitespace trimmed

    def __len__(self) -> int:
        return len(self.input_ids)


def trim_offsets(text: str, offsets: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for a, b in offsets:
        while a < b and text[a].isspace():
            a += 1
        out.append((a, b))
    return out


def window_bounds(n: int, window: int, stride: int) -> list[tuple[int, int]]:
    """Token windows [start, end) of size ``window`` advancing by ``stride``."""
    if n <= window:
        return [(0, n)]
    bounds = []
    start = 0
    while True:
        end = min(start + window, n)
        bounds.append((start, end))
        if end == n:
            return bounds
        start += stride


def torch_dtype(name: str):
    import torch

    return {"fp32": torch.float32, "bf16": torch.bfloat16, "fp16": torch.float16}[name]


def is_local_model(model_id: str) -> bool:
    return os.path.isdir(model_id)


class Extractor:
    def __init__(
        self,
        model_id: str,
        revision: str | None = None,
        device: str = "cpu",
        dtype: str = "bf16",
        window: int = 512,
        stride: int | None = None,
        batch_size: int = 8,
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if dtype not in DTYPE_NAMES:
            raise ValueError(f"dtype must be one of {DTYPE_NAMES}")
        self.model_id = model_id
        self.revision = revision
        self.device = device
        self.dtype = dtype
        self.window = window
        self.stride = stride if stride is not None else max(1, window * 3 // 4)
        self.batch_size = batch_size
        self.fp32_fallback = False
        rev = None if is_local_model(model_id) else revision
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, revision=rev)
        model = AutoModelForCausalLM.from_pretrained(model_id, revision=rev, dtype=torch_dtype(dtype))
        self.model = model.eval().to(device)
        self.layers = self.model.model.layers
        self.num_layers = len(self.layers)
        self.hidden_size = int(self.model.config.hidden_size)
        pad = self.tokenizer.pad_token_id
        self.pad_id = pad if pad is not None else (self.tokenizer.eos_token_id or 0)

    # tokenization

    def tokenize(self, text: str) -> Tokenized:
        enc = self.tokenizer(text, return_offsets_mapping=True, add_special_tokens=False)
        return Tokenized(list(enc["input_ids"]), trim_offsets(text, enc["offset_mapping"]))

    # forward

    def run_batch(self, seqs: Sequence[Sequence[int]], layers: Sequence[int]) -> list[dict[int, "np.ndarray"]]:
        """Run right-padded sequences, return per-sequence {layer: (T, H) float32}."""
        out = self._run_batch_torch(seqs, layers)
        return [{k: v.float().cpu().numpy() for k, v in d.items()} for d in out]

    def _run_batch_torch(self, seqs, layers):
        import torch

        layers = sorted(set(int(k) for k in layers))
        if not layers or layers[0] < 0 or layers[-1] >= self.num_layers:
            raise ValueError(f"layers must be in [0, {self.num_layers})")
        deepest = layers[-1]
        lens = [len(s) for s in seqs]
        T = max(lens)
        ids = torch.full((len(seqs), T), self.pad_id, dtype=torch.long)
        mask = torch.zeros((len(seqs), T), dtype=torch.long)
        for i, s in enumerate(seqs):
            ids[i, : len(s)] = torch.tensor(list(s), dtype=torch.long)
            mask[i, : len(s)] = 1
        captured: dict[int, torch.Tensor] = {}

        def make_hook(k):
            def hook(_mod, _inp, out):
                captured[k] = (out[0] if isinstance(out, tuple) else out).detach()
                if k == deepest:
                    raise _StopForward

            return hook

        handles = [self.layers[k].register_forward_hook(make_hook(k)) for k in layers]
        try:
            with torch.no_grad():
                try:
                    self.model.model(input_ids=ids.to(self.device), attention_mask=mask.to(self.device), use_cache=False)
                except _StopForward:
                    pass
        finally:
            for h in handles:
                h.remove()

        if any(torch.isnan(captured[k]).any().item() for k in layers) and self.dtype != "fp32":
            log.warning("NaN activations in %s; falling back to fp32 for this model", self.dtype)
            self.model = self.model.float()
            self.dtype = "fp32"
            self.fp32_fallback = True
            return self._run_batch_torch(seqs, layers)
        return [{k: captured[k][i, :n] for k in layers} for i, n in enumerate(lens)]

    def iter_windows(self, tok: Tokenized, layers: Sequence[int]) -> Iterator[tuple[int, int, dict[int, np.ndarray]]]:
        """Yield (start, end, {layer: (end-start, H)}) for each window of a tokenized text."""
        bounds = window_bounds(len(tok), self.window, self.stride)
        for i in range(0, len(bounds), self.batch_size):
            chunk = bounds[i : i + self.batch_size]
            outs = self.run_batch([tok.input_ids[a:b] for a, b in chunk], layers)
            for (a, b), acts in zip(chunk, outs):
                yield a, b, acts

    def extract(self, text: str, layers: Sequence[int]) -> tuple[Tokenized, dict[int, np.ndarray]]:
        """Per-token activations, taking each token from the first window that holds it."""
        tok = self.tokenize(text)
        layers = sorted(set(layers))
        result = {k: np.zeros((len(tok), self.hidden_size), dtype=np.float32) for k in layers}
        filled = 0
        for a, b, acts in self.iter_windows(tok, layers):
            if b <= filled:
                continue
            lo = filled - a
            for k in layers:
                result[k][filled:b] = acts[k][lo:]
            filled = b
        return tok, result

    def extract_many(self, texts: Sequence[str], layers: Sequence[int]) -> Iterator[tuple[Tokenized, dict[int, np.ndarray]]]:
        """Like ``extract`` but batches short texts together for speed."""
        layers = sorted(set(layers))
        pending: list[tuple[int, Tokenized]] = []
        results: dict[int, tuple[Tokenized, dict[int, np.ndarray]]] = {}
        next_out = 0

        def flush():
            outs = self.run_batch([t.input_ids for _, t in pending], layers)
            for (j, t), acts in zip(pending, outs):
                results[j] = (t, acts)
            pending.clear()

        for j, text in enumerate(texts):
            tok = self.tokenize(text)
            if len(tok) == 0:
                results[j] = (tok, {k: np.zeros((0, self.hidden_size), np.float32) for k in layers})
            elif len(tok) > self.window:
                results[j] = self.extract(text, layers)
            else:
                pending.append((j, tok))
                if len(pending) >= self.batch_size:
                    flush()
            while next_out in results:
                yield results.pop(next_out)
                next_out += 1
        if pending:
            flush()
        while next_out in results:
            yield results.pop(next_out)
            next_out += 1


def evenly_spaced_layers(num_layers: int, n: int = 8) -> list[int]:
    if n >= num_layers:
        return list(range(num_layers))
    return sorted(set(int(round(x)) for x in np.linspace(0, num_layers - 1, n)))
