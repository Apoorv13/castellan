"""8-gram overlap between training data and each frozen eval set (a measurement only).

    python scripts/leakage.py --train data_out/train_s0_n12000.jsonl --out runs/leakage.json

For each eval doc: the fraction of its 8-grams that also occur anywhere in
the training texts. Two granularities:
- word: whitespace-split tokens (template and phrasing overlap)
- qwen: Qwen3 tokenizer ids (what the model sees), if the tokenizer is available

Reports mean, max, p95 and the share of docs above 0.5 per set. Prints numbers only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np

from castellan.data.schema import read_jsonl

SETS = ["dev", "secrets_eval", "hardneg", "longctx", "final", "ladder", "ladder_neutral"]
N = 8


def grams(seq, n=N):
    return {tuple(seq[i : i + n]) for i in range(len(seq) - n + 1)}


def overlap_stats(train_grams: set, docs: list) -> dict:
    fr = []
    skipped = 0
    for seq in docs:
        g = grams(seq)
        if not g:
            skipped += 1
            continue
        fr.append(len(g & train_grams) / len(g))
    a = np.array(fr)
    return {
        "n_docs": len(docs), "n_scored": len(fr), "n_too_short": skipped,
        "mean": float(a.mean()) if len(a) else None, "max": float(a.max()) if len(a) else None,
        "p95": float(np.percentile(a, 95)) if len(a) else None,
        "share_above_0.5": float((a > 0.5).mean()) if len(a) else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--out", default="runs/leakage.json")
    ap.add_argument("--eval-dir", default="eval_data")
    ap.add_argument("--tokenizer", default="Qwen/Qwen3-0.6B")
    ap.add_argument("--revision", default="c1899de289a04d12100db370d81485cdf75e47ca")
    args = ap.parse_args()

    train_texts = [e.text for e in read_jsonl(args.train)]
    tok = None
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(args.tokenizer, revision=args.revision)
    except Exception as e:  # tokenizer missing offline: word level only
        print(json.dumps({"warning": f"no tokenizer ({type(e).__name__}); word level only"}))

    enc = {"word": lambda t: t.split()}
    if tok is not None:
        enc["qwen"] = lambda t: tok(t, add_special_tokens=False)["input_ids"]
    report = {"train": args.train, "n": N, "train_docs": len(train_texts), "sets": {}}
    for name, f in enc.items():
        tg: set = set()
        for t in train_texts:
            tg |= grams(f(t))
        report.setdefault("train_grams", {})[name] = len(tg)
        for s in SETS:
            docs = [f(e.text) for e in read_jsonl(Path(args.eval_dir) / f"{s}.jsonl")]
            report["sets"].setdefault(s, {})[name] = overlap_stats(tg, docs)
            print(json.dumps({"level": name, "set": s, **report["sets"][s][name]}), flush=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
