"""Section 7 gate check: probe vs baselines on dev, secrets_eval and hardneg ONLY.

    python scripts/gate_eval.py --bundle bundles/q17_s0 --baselines runs/baselines --out runs/gate_q17_s0.json

Runs the probe on the three gate sets (never ladder, ladder_neutral or
final), loads cached baseline predictions from run_baselines.py, and
checks the gate:
  1. dev and secrets_eval: probe secret span recall > Gitleaks (defaults),
     at equal or better span precision.
  2. hardneg, labels ignored: char_fpr <= 0.005 and neg_doc_flag_rate <= 0.05.
Non-blocking, information only (user decision 2026-11-16): dev and
secrets_eval probe pattern-PII span recall > Presidio + UK NINO at equal or
better span precision. That baseline's recall is 1.0, so the line cannot be
met; it is written under "info" with the probe's FPR at the baseline's recall,
and never affects gate_passed.
Also writes matched operating points against each baseline. Prints numbers only.
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
from castellan.data.schema import read_jsonl  # noqa: E402
from castellan.evaluate import compare_operating_points, evaluate, timed_predict  # noqa: E402

GATE_SETS = ("dev", "secrets_eval", "hardneg")
BASELINES = ("gitleaks", "gitleaks_d0", "presidio")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--baselines", default="runs/baselines")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--out", required=True)
    ap.add_argument("--eval-dir", default="eval_data")
    ap.add_argument("--detector", default=None, help="module:factory(bundle, bundle_path), e.g. castellan.stage2:detector_factory")
    args = ap.parse_args()

    b = load_bundle(args.bundle, device=args.device)
    if args.detector:
        import importlib

        mod, fn = args.detector.split(":")
        det = getattr(importlib.import_module(mod), fn)(b, args.bundle)
    else:
        det = ProbeDetector(b)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    report = {"git_sha": sha, "bundle": args.bundle, "detector": args.detector or "probe", "model_id": b.model_id, "revision": b.revision,
              "dtype": b.dtype, "layer": b.layer, "train_data_hash": b.meta.get("data_hash"),
              "seed": b.meta.get("seed"), "sets": {}}
    preds_dir = Path(args.out).with_suffix("")
    preds_dir.mkdir(parents=True, exist_ok=True)
    for s in GATE_SETS:
        exs = list(read_jsonl(Path(args.eval_dir) / f"{s}.jsonl"))
        t = timed_predict(det.predict, exs, lambda x: len(b.extractor.tokenize(x)))
        save_preds(preds_dir / f"{s}__probe.json", t.preds)
        entry = {"probe": {"metrics": evaluate(exs, t.preds),
                           "timing": {"ms_per_1k_tokens": t.ms_per_1k_tokens, "n_tokens": t.n_tokens,
                                      "seconds": t.seconds, "peak_rss_bytes": t.peak_rss_bytes,
                                      "mps_driver_bytes": t.mps_driver_bytes}},
                 "comparisons": {}}
        for bn in BASELINES:
            bp = load_preds(Path(args.baselines) / "preds" / f"{s}__{bn}.json")
            entry[bn] = {"metrics": json.loads((Path(args.baselines) / f"{s}__{bn}.json").read_text())["metrics"]}
            entry["comparisons"][bn] = compare_operating_points(exs, t.preds, bp)
        report["sets"][s] = entry
        print(json.dumps({"set": s, "seconds": round(t.seconds, 1), "ms_per_1k_tokens": round(t.ms_per_1k_tokens, 1)}),
              flush=True)

    def cat(s, d, c):
        return report["sets"][s][d]["metrics"]["categories"][c]

    gate, info = {}, {}
    for s in ("dev", "secrets_eval"):
        p, g = cat(s, "probe", "secret"), cat(s, "gitleaks", "secret")
        gate[f"{s}:secrets_vs_gitleaks"] = bool(
            p["span_recall"] > g["span_recall"] and (p["span_precision"] or 0) >= (g["span_precision"] or 0))
        p, q = cat(s, "probe", "pattern_pii"), cat(s, "presidio", "pattern_pii")
        info[f"{s}:pattern_pii_vs_presidio_uk_nino"] = {
            "blocking": False,
            "as_written": bool(p["span_recall"] > q["span_recall"]
                               and (p["span_precision"] or 0) >= (q["span_precision"] or 0)),
            "probe_recall": p["span_recall"], "baseline_recall": q["span_recall"],
            "probe_precision": p["span_precision"], "baseline_precision": q["span_precision"],
            "matched_points": report["sets"][s]["comparisons"]["presidio"],
        }
    hn = report["sets"]["hardneg"]["probe"]["metrics"]["any_label"]
    gate["hardneg:char_fpr<=0.005"] = bool(hn["char_fpr"] is not None and hn["char_fpr"] <= 0.005)
    gate["hardneg:neg_doc_flag_rate<=0.05"] = bool(hn["neg_doc_flag_rate"] <= 0.05)
    report["gate"] = gate
    report["info"] = info
    report["gate_passed"] = all(gate.values())
    Path(args.out).write_text(json.dumps(report, indent=1, default=float))
    print(json.dumps({"gate": gate, "passed": report["gate_passed"],
                      "info_non_blocking": {k: v["as_written"] for k, v in info.items()}}, indent=1))


if __name__ == "__main__":
    main()
