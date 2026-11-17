"""Counts-only breakdown of false-positive secret spans on dev (no text, no offsets printed).

    python scripts/fp_diag.py runs/dev_e2s2.preds.json

A predicted secret span is a false positive when it overlaps no gold secret span
(same rule as evaluate.py: at least one shared char, same category).
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from castellan.data.schema import read_jsonl  # noqa: E402
from castellan.evaluate import SECRET_LABELS  # noqa: E402
from run_baselines import load_preds  # noqa: E402

DEV = "eval_data/dev.jsonl"


def lb(n: int) -> str:
    for cut in (16, 32, 64, 128, 256, 512):
        if n < cut:
            return f"<{cut}"
    return ">=512"


def main() -> None:
    gold = {e.id: e for e in read_jsonl(DEV)}
    out = Counter()
    for p in load_preds(Path(sys.argv[1])):
        e = gold[p.id]
        gs = [(s.start, s.end) for s in e.spans if s.label in SECRET_LABELS]
        where = "pos_doc" if e.spans else "neg_doc"
        for f in p.findings:
            if f.label not in SECRET_LABELS:
                continue
            tp = any(f.start < b and a < f.end for a, b in gs)
            kind = "tp" if tp else "fp"
            out[f"{kind}:{where}"] += 1
            out[f"{kind}_len:{lb(f.end - f.start)}"] += 1
            out[f"{kind}_label:{f.label}"] += 1
            out[f"{kind}_chars:{where}"] += f.end - f.start
    print(json.dumps(dict(sorted(out.items())), indent=1))


if __name__ == "__main__":
    main()
