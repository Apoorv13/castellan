"""Model-can-read scorer for ladder items.

For each secret item, prompt the model (chat template, thinking off, greedy)
to output the reconstructed secret, then score an exact match after
normalization against the item's ``target_sha256``. The target is the SHA-256
of the secret with all whitespace removed, so the comparison never needs the
secret value itself.

Normalization of the model output:
- Candidates: the whole output, the output without code fences, the output
  after a leading "label:" line, each line, and each backtick- or
  quote-wrapped run. Wrapping quotes, backticks and a trailing period are
  stripped.
- All whitespace is removed (the same rule as the target).
- No Unicode folding and no zero-width removal: echoing the obfuscated text
  (homoglyphs, zero-width characters, spelled-out words) does not count as
  reading it.

``model_can_read`` is stored per (item, model) with token counts only. The
model's output is never written, printed or logged. Failure modes are kept
apart as booleans: ``empty`` (nothing left after stripping), ``refusal``
(refusal phrasing, see REFUSAL), ``think_emitted`` (a think block appeared
despite enable_thinking=False; it is stripped before scoring) and
``truncated`` (hit max_new_tokens without EOS).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from castellan.data.schema import Example

log = logging.getLogger(__name__)

# v1 (used for the first 1.7B run, which failed the rung-0 sanity gate; kept for the record).
PROMPT_V1 = (
    "The text below contains one secret value, such as an API key, access token, password, private key or "
    "database connection string. It may be split into pieces, encoded, obfuscated with look-alike or invisible "
    "characters, spelled out in words, or spread across several messages. Reconstruct the original secret "
    "exactly and reply with the secret only, with no other words.\n\n<text>\n{text}\n</text>"
)
# v2 (user-approved instrument fix, 2026-11-16): ask for the complete value exactly as it would be used.
# Only the reconstruction sentence changed.
PROMPT = (
    "The text below contains one secret value, such as an API key, access token, password, private key or "
    "database connection string. It may be split into pieces, encoded, obfuscated with look-alike or invisible "
    "characters, spelled out in words, or spread across several messages. Reconstruct the complete original "
    "secret exactly as it would be used: for a connection string, the full string including the scheme, user, "
    "password and host; for a private key, the full key block including the BEGIN and END lines. Reply with "
    "the secret only, with no other words.\n\n<text>\n{text}\n</text>"
)
PROMPT_VERSION = "v2"

_WS = re.compile(r"\s+")
_FENCE = re.compile(r"^```[^\n]*\n?|\n?```\s*$")
_WRAPPED = re.compile(r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'")
_STRIP = "\"'`.,;"
_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)
REFUSAL = re.compile(
    r"\b(I can(?:no|')t|I cannot|I'm sorry|I am sorry|I'm unable|I am unable|I won't|I will not|"
    r"not able to (?:help|assist|provide)|can(?:no|')t (?:help|assist|provide|share|reveal)|"
    r"against my (?:guidelines|policy)|as an AI)\b", re.I)


def strip_think(output: str) -> tuple[str, bool]:
    stripped = _THINK.sub("", output)
    return stripped, stripped != output


def is_refusal(output: str) -> bool:
    return bool(REFUSAL.search(output))


def _norm(s: str) -> str:
    return _WS.sub("", s).strip(_STRIP)


def candidates(output: str) -> set[str]:
    out = output.strip()
    c = {out, _FENCE.sub("", out)}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) > 1 and lines[0].rstrip().endswith(":"):
        c.add("\n".join(lines[1:]))
    c.update(lines)
    for m in _WRAPPED.finditer(out):
        c.add(next(g for g in m.groups() if g is not None))
    for ln in lines:
        if ":" in ln:
            c.add(ln.split(":", 1)[1])
    return {n for n in (_norm(x) for x in c) if n}


def levenshtein(a: str, b: str) -> int:
    """Edit distance. Uses rapidfuzz (extra: analysis) when installed, else a plain-Python fallback."""
    try:
        from rapidfuzz.distance import Levenshtein
    except ImportError:
        return _levenshtein_py(a, b)
    return Levenshtein.distance(a, b)


def _levenshtein_py(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def norm_edit_distance(output: str, target: str) -> float:
    """Min over candidates of Levenshtein(candidate, target) / max(len). 0 = exact, 1 = nothing shared."""
    t = _WS.sub("", target)
    best = 1.0
    for c in candidates(output) or {""}:
        d = levenshtein(c, t) / max(len(c), len(t), 1)
        best = min(best, d)
    return best


def can_read(output: str, target_sha256: str) -> bool:
    return any(hashlib.sha256(n.encode()).hexdigest() == target_sha256 for n in candidates(output))


@dataclass
class ReadResult:
    id: str
    model: str
    revision: str
    rung: int | None
    model_can_read: bool
    empty: bool
    refusal: bool
    think_emitted: bool
    truncated: bool
    edit_dist_norm: float | None
    prompt_version: str
    n_prompt_tokens: int
    n_new_tokens: int
    hit_eos: bool


def secret_items(examples: Iterable[Example]) -> list[Example]:
    return [e for e in examples if (e.meta or {}).get("kind") == "secret" and (e.meta or {}).get("target_sha256")]


def run(
    examples: Sequence[Example],
    model_id: str,
    revision: str,
    out: str | Path,
    device: str = "mps",
    dtype: str = "bf16",
    batch_size: int = 8,
    max_new_tokens: int = 1024,
) -> dict:
    """Score every secret item; append one JSON line per item to ``out``. Resumes by id."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(ln)["id"] for ln in out.read_text().splitlines() if ln.strip()}
    todo = [e for e in secret_items(examples) if e.id not in done]
    # Target text per hash, for the edit-distance column only: the rung-0 gold value of each base
    # (the target is that value with whitespace removed). Held in memory, never written.
    tgt_text = {e.meta["target_sha256"]: "".join(e.text[x.start:x.end] for x in e.spans)
                for e in secret_items(examples) if e.rung == 0}
    tok = AutoTokenizer.from_pretrained(model_id, revision=revision)
    tok.padding_side = "left"
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tdt = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[dtype]
    model = AutoModelForCausalLM.from_pretrained(model_id, revision=revision, dtype=tdt).to(device).eval()
    prompts = [tok.apply_chat_template([{"role": "user", "content": PROMPT.format(text=e.text)}], tokenize=False,
                                       add_generation_prompt=True, enable_thinking=False) for e in todo]
    lens = [len(tok(p, add_special_tokens=False)["input_ids"]) for p in prompts]
    order = sorted(range(len(todo)), key=lambda i: lens[i])
    t0 = time.time()
    n_done = 0
    with out.open("a") as fh:
        for k in range(0, len(order), batch_size):
            idx = order[k: k + batch_size]
            enc = tok([prompts[i] for i in idx], return_tensors="pt", padding=True, add_special_tokens=False).to(device)
            with torch.no_grad():
                gen = model.generate(**enc, do_sample=False, max_new_tokens=max_new_tokens,
                                     pad_token_id=tok.pad_token_id, temperature=None, top_p=None, top_k=None)
            new = gen[:, enc["input_ids"].shape[1]:].cpu()
            for row, i in zip(new, idx):
                ids = row.tolist()
                eos = tok.eos_token_id in ids or tok.pad_token_id in ids
                n_new = next((j for j, t in enumerate(ids) if t in (tok.eos_token_id, tok.pad_token_id)), len(ids))
                text, think = strip_think(tok.decode(ids[:n_new], skip_special_tokens=True))
                e = todo[i]
                ok = can_read(text, e.meta["target_sha256"])
                r = ReadResult(e.id, model_id, revision, e.rung, ok, empty=not candidates(text),
                               refusal=(not ok) and is_refusal(text), think_emitted=think,
                               truncated=not eos,
                               edit_dist_norm=(0.0 if ok else norm_edit_distance(text, tgt_text[e.meta["target_sha256"]])
                                               if e.meta["target_sha256"] in tgt_text else None),
                               prompt_version=PROMPT_VERSION,
                               n_prompt_tokens=lens[i], n_new_tokens=n_new, hit_eos=eos)
                del text
                fh.write(json.dumps(asdict(r)) + "\n")
            fh.flush()
            n_done += len(idx)
            log.info("readability %s: %d/%d items, %.0f s", model_id, n_done, len(todo), time.time() - t0)
    rows = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    return {"model": model_id, "revision": revision, "n_items": len(rows),
            "can_read_rate": sum(r["model_can_read"] for r in rows) / max(len(rows), 1), "seconds": time.time() - t0}
