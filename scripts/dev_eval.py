"""Loop evaluation on eval_data/dev.jsonl ONLY (docs/DESIGN.md section 9).

    python scripts/dev_eval.py --bundle bundles/q17_s0 --out runs/dev_q17_s0.json

Never pass any other eval set to this script. It reads dev, runs the probe,
loads cached dev baseline predictions from run_baselines.py, and prints
aggregate numbers only (no values, no per-document detail).

Loop score (pre-registered 2026-11-16 in STATE.md): secret span precision on
dev, counted only when secret span recall beats gitleaks' dev span recall;
otherwise the score is 0. A gain of 1 point means +0.01.

Dev-negative block: the docs in dev with no gold spans of any label, scored
with labels ignored. It is a proxy for the hardneg gate lines and is never a
substitute for running the gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_baselines import load_preds, save_preds  # noqa: E402

from castellan.bundle import ProbeDetector, load_bundle  # noqa: E402
from castellan.cache import dataset_hash  # noqa: E402
from castellan.data.schema import read_jsonl  # noqa: E402
from castellan.evaluate import compare_operating_points, evaluate, timed_predict  # noqa: E402

DEV = Path("eval_data/dev.jsonl")


def summarize(m: dict, cat: str) -> dict:
    c = m["categories"][cat]
    keys = ("span_precision", "span_recall", "span_f2", "exact_recall", "char_precision", "char_recall",
            "doc_auroc", "doc_recall_at_0.01_fpr")
    return {k: c.get(k) for k in keys}


def loop_score(probe_secret: dict, gl_secret: dict) -> float:
    if probe_secret["span_recall"] is None or probe_secret["span_recall"] <= gl_secret["span_recall"]:
        return 0.0
    return float(probe_secret["span_precision"] or 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--baselines", default="runs/baselines")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--out", required=True)
    ap.add_argument("--detector", default=None, help="python path module:factory(bundle) for a custom detector")
    args = ap.parse_args()

    exs = list(read_jsonl(DEV))
    b = load_bundle(args.bundle, device=args.device)
    if args.detector:
        mod, fn = args.detector.split(":")
        det = getattr(__import__(mod, fromlist=[fn]), fn)(b, args.bundle)
    else:
        det = ProbeDetector(b)
    t = timed_predict(det.predict, exs, lambda x: len(b.extractor.tokenize(x)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_preds(out.with_suffix(".preds.json"), t.preds)

    m = evaluate(exs, t.preds)
    neg = [e for e in exs if not e.spans]
    neg_ids = {e.id for e in neg}
    neg_m = evaluate(neg, [p for p in t.preds if p.id in neg_ids])["any_label"]
    gl = json.loads((Path(args.baselines) / "dev__gitleaks.json").read_text())["metrics"]
    pr = json.loads((Path(args.baselines) / "dev__presidio.json").read_text())["metrics"]
    gl_preds = load_preds(Path(args.baselines) / "preds" / "dev__gitleaks.json")
    comp = compare_operating_points(exs, t.preds, gl_preds)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True).stdout.strip())
    ps = summarize(m, "secret")
    gs = summarize(gl, "secret")
    report = {
        "git_sha": sha, "git_dirty": dirty, "bundle": args.bundle, "detector": args.detector or "probe",
        "model_id": b.model_id, "revision": b.revision, "dtype": b.dtype, "layer": b.layer,
        "train_data_hash": b.meta.get("data_hash"), "seed": b.meta.get("seed"),
        "dev_hash": dataset_hash(exs),
        "loop_score": loop_score(ps, gs),
        "secret": {"probe": ps, "gitleaks": gs},
        "pattern_pii": {"probe": summarize(m, "pattern_pii"), "presidio_uk_nino": summarize(pr, "pattern_pii")},
        "dev_negatives_any_label": {"char_fpr": neg_m["char_fpr"], "neg_doc_flag_rate": neg_m["neg_doc_flag_rate"],
                                    "n_neg_docs": neg_m["n_neg_docs"]},
        "matched_vs_gitleaks_secret": comp["categories"]["secret"],
        "per_class": {k: {kk: v.get(kk) for kk in ("span_precision", "span_recall")}
                      for k, v in m["per_class"].items() if k.startswith("SECRET")},
        "timing": {"ms_per_1k_tokens": t.ms_per_1k_tokens, "seconds": t.seconds,
                   "peak_rss_bytes": t.peak_rss_bytes, "mps_driver_bytes": t.mps_driver_bytes},
        "full_metrics": m,
        "matched_full": comp,
    }
    out.write_text(json.dumps(report, indent=1, default=float))
    brief = {k: report[k] for k in ("loop_score", "secret", "dev_negatives_any_label", "matched_vs_gitleaks_secret")}
    brief["pattern_pii_probe"] = report["pattern_pii"]["probe"]
    brief["ms_per_1k_tokens"] = round(t.ms_per_1k_tokens, 1)
    print(json.dumps(brief, indent=1, default=float))


if __name__ == "__main__":
    main()
