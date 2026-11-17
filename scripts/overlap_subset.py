"""Two follow-up checks on the lookalike overlap (user-allowed, 2026-11-16). Counts and metrics only.

1. Do the unexplained shared 40-char windows (see overlap_check.py) also occur in
   synth.py training output? Counts only.
2. l03 vs l12 on dev split into docs sharing no 40-char substring with any
   lookalike doc and docs sharing one: secret span P/R (evaluate.count_doc,
   category level) and dev-negative char FPR and doc flag rate (any label),
   with bootstrap 95% CIs over documents.

    python scripts/overlap_subset.py
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import overlap_check as O  # noqa: E402
from castellan.data.schema import read_jsonl  # noqa: E402
from castellan.evaluate import CATEGORIES, _cat_same, count_doc  # noqa: E402
from run_baselines import load_preds  # noqa: E402

LK = ["data_out/lk_n5000_jwtcap.jsonl", "data_out/lk_n5000_nodoc.jsonl"]
SYNTH = "data_out/train_s0_n12000.jsonl"
K = O.K
B = 2000


def shingles(t: str):
    for i in range(len(t) - K + 1):
        yield O.h(t[i:i + K]), i


def main() -> None:
    lk, lk_txt = set(), {}
    for p in LK:
        for e in read_jsonl(p):
            for x, i in shingles(e.text):
                lk.add(x)
                lk_txt.setdefault(x, e.text[i:i + K])
    fixture = {O.h(v[i:i + K]) for v in O.DOC_EXAMPLE_VALUES for i in range(len(v) - K + 1)}

    # Unexplained windows that occur in any eval set
    unexpl = set()
    for name in O.EVAL:
        for e in read_jsonl(f"eval_data/{name}.jsonl"):
            for x, _ in shingles(e.text):
                if x in lk and x not in fixture and O.explain(lk_txt[x]) == "unexplained":
                    unexpl.add(x)
    synth_sh = set()
    for e in read_jsonl(SYNTH):
        for x, _ in shingles(e.text):
            if x in unexpl:
                synth_sh.add(x)
    res = {"check1": {"unique_unexplained_windows": len(unexpl), "also_in_synth_output": len(synth_sh),
                      "not_in_synth_output": len(unexpl - synth_sh)}}

    # Dev split by overlap with any lookalike doc
    dev = list(read_jsonl("eval_data/dev.jsonl"))
    overl = np.array([any(x in lk for x, _ in shingles(e.text)) for e in dev])
    neg = np.array([not e.spans for e in dev])
    sec = CATEGORIES["secret"]
    rng = np.random.default_rng(0)
    res["check2"] = {"n_dev": len(dev), "n_overlap": int(overl.sum()), "n_overlap_neg": int((overl & neg).sum()),
                     "n_free": int((~overl).sum()), "n_free_neg": int((~overl & neg).sum()), "runs": {}}
    for run in ("l03", "l12"):
        by = {p.id: p for p in load_preds(Path(f"runs/dev_{run}.preds.json"))}
        # per-doc counts: pred, pred_hit, gold, gold_hit (secret); neg pred chars, text chars, flagged (any label)
        A = np.zeros((len(dev), 7))
        for i, e in enumerate(dev):
            p = by[e.id]
            g = [(s.start, s.end, s.label) for s in e.spans if s.label in sec]
            q = [(f.start, f.end, f.label) for f in p.findings if f.label in sec]
            c = count_doc(len(e.text), g, q, _cat_same)
            a = count_doc(len(e.text), [(s.start, s.end, s.label) for s in e.spans],
                          [(f.start, f.end, f.label) for f in p.findings], lambda u, v: True)
            A[i] = [c.pred, c.pred_hit, c.gold, c.gold_hit, a.pred_chars - a.overlap_chars,
                    a.text_chars - a.gold_chars, float(bool(p.findings))]

        def metrics(idx):
            S = A[idx].sum(0)
            ni = idx[neg[idx]]
            N = A[ni].sum(0)
            return np.array([S[1] / max(S[0], 1), S[3] / max(S[2], 1), N[4] / max(N[5], 1),
                             A[ni, 6].mean() if len(ni) else np.nan])

        out = {}
        for sub, mask in (("all", np.ones(len(dev), bool)), ("free", ~overl), ("overlap", overl)):
            idx = np.flatnonzero(mask)
            point = metrics(idx)
            bs = np.array([metrics(rng.choice(idx, len(idx), replace=True)) for _ in range(B)])
            lo, hi = np.nanpercentile(bs, 2.5, axis=0), np.nanpercentile(bs, 97.5, axis=0)
            out[sub] = {k: [round(float(point[j]), 4), round(float(lo[j]), 4), round(float(hi[j]), 4)]
                        for j, k in enumerate(["secret_span_P", "secret_span_R", "neg_char_fpr", "neg_doc_flag_rate"])}
        res["check2"]["runs"][run] = out
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
