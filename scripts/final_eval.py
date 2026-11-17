"""Held-out scoring of probe-v1 on eval_data/final.jsonl. Run ONCE (user, 2026-11-17). Nothing changes after it.

    python scripts/final_eval.py --out runs/final_probe-v1.json

- Checks final.jsonl against eval_data/FROZEN.sha256 and refuses to run on a mismatch or if --out exists.
- Finds the final documents that share a non-fixture 40-char window with a hash_digest lookalike in
  data_out/lk_n10000_nodoc.jsonl (the open limitation logged on 2026-11-16). Only ids are kept; no text.
- Runs probe-v1 (1.7B, two-stage) once; baselines come from cached predictions (Gitleaks defaults,
  Gitleaks depth 0, Presidio + UK NINO).
- Reports, for all documents and without the overlap document(s): secret span P/R, doc recall at 1% FPR,
  probe recall at Gitleaks' FPR, and on the negative documents (no gold spans, labels ignored) char FPR
  and doc flag rate. The same block is computed for dev from cached predictions, for the side-by-side.
Prints aggregate numbers only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from overlap_check import K, explain, h  # noqa: E402
from run_baselines import load_preds, save_preds  # noqa: E402

from castellan.bundle import load_bundle  # noqa: E402
from castellan.data.lookalikes import DOC_EXAMPLE_VALUES  # noqa: E402
from castellan.data.schema import read_jsonl  # noqa: E402
from castellan.evaluate import compare_operating_points, evaluate, timed_predict  # noqa: E402
from castellan.stage2 import detector_factory  # noqa: E402

BUNDLE = "bundles/probe-v1"
BASELINES = ("gitleaks", "gitleaks_d0", "presidio")


def frozen_ok(path: str) -> bool:
    want = {ln.split()[1]: ln.split()[0] for ln in Path("eval_data/FROZEN.sha256").read_text().splitlines() if ln.strip()}
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() == want[path]


def overlap_ids(final) -> list[str]:
    fixture = {h(v[i:i + K]) for v in DOC_EXAMPLE_VALUES for i in range(len(v) - K + 1)}
    sh = set()
    for e in read_jsonl("data_out/lk_n10000_nodoc.jsonl"):
        if e.meta.get("category") != "hash_digest":
            continue
        t = e.text
        for i in range(len(t) - K + 1):
            x = h(t[i:i + K])
            if x not in fixture:
                sh.add(x)
    out = []
    for e in final:
        t = e.text
        for i in range(len(t) - K + 1):
            s = t[i:i + K]
            if h(s) in sh and explain(s) not in ("format_constant", "fixture_adjacent"):
                out.append(e.id)
                break
    return out


def block(exs, probe_preds, base_preds) -> dict:
    ids = {e.id for e in exs}
    pp = [p for p in probe_preds if p.id in ids]
    neg = [e for e in exs if not e.spans]
    nid = {e.id for e in neg}
    res = {"n_docs": len(exs), "n_neg_docs": len(neg)}
    for name, preds in [("probe", pp)] + [(b, [p for p in base_preds[b] if p.id in ids]) for b in BASELINES]:
        m = evaluate(exs, preds)
        s = m["categories"]["secret"]
        nm = evaluate(neg, [p for p in preds if p.id in nid])["any_label"]
        res[name] = {"span_precision": s["span_precision"], "span_recall": s["span_recall"],
                     "exact_recall": s["exact_recall"], "doc_auroc": s["doc_auroc"],
                     "doc_recall_at_0.01_fpr": s["doc_recall_at_0.01_fpr"],
                     "neg_char_fpr": nm["char_fpr"], "neg_doc_flag_rate": nm["neg_doc_flag_rate"]}
    comp = compare_operating_points(exs, pp, [p for p in base_preds["gitleaks"] if p.id in ids])
    res["probe_at_gitleaks_fpr"] = comp["categories"]["secret"]
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="mps")
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        sys.exit("final already scored; refusing to run again")
    if not frozen_ok("eval_data/final.jsonl"):
        sys.exit("final.jsonl does not match FROZEN.sha256")
    final = list(read_jsonl("eval_data/final.jsonl"))
    ov = overlap_ids(final)
    print(json.dumps({"n_final": len(final), "n_overlap_docs": len(ov)}), flush=True)

    b = load_bundle(BUNDLE, device=a.device)
    det = detector_factory(b, BUNDLE)
    t = timed_predict(det.predict, final, lambda x: len(b.extractor.tokenize(x)))
    save_preds(out.with_suffix(".preds.json"), t.preds)
    fb = {n: load_preds(Path(f"runs/baselines/preds/final__{n}.json")) for n in BASELINES}

    dev = list(read_jsonl("eval_data/dev.jsonl"))
    db = {n: load_preds(Path(f"runs/baselines/preds/dev__{n}.json")) for n in BASELINES}
    dev_probe = load_preds(Path("runs/dev_l17.preds.json"))

    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    rep = {"git_sha": sha, "bundle": BUNDLE, "model_id": b.model_id, "revision": b.revision, "dtype": b.dtype,
           "layer": b.layer, "train_data_hash": b.meta.get("data_hash"), "seed": b.meta.get("seed"),
           "final_sha256_matches_frozen": True, "overlap_doc_ids": ov,
           "overlap_doc_has_gold_spans": [bool(e.spans) for e in final if e.id in set(ov)],
           "final_all": block(final, t.preds, fb),
           "final_without_overlap": block([e for e in final if e.id not in set(ov)], t.preds, fb),
           "dev": block(dev, dev_probe, db),
           "timing": {"ms_per_1k_tokens": t.ms_per_1k_tokens, "seconds": t.seconds}}
    out.write_text(json.dumps(rep, indent=1, default=float))
    print(json.dumps({k: rep[k] for k in ("final_all", "final_without_overlap", "dev")}, indent=1, default=float))


if __name__ == "__main__":
    main()
