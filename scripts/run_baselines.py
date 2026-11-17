"""Run the baselines on every frozen eval set and write reports.

    python scripts/run_baselines.py --out runs/baselines

For each (set, detector) this writes:
    <out>/preds/<set>__<detector>.json   findings as offsets, labels, scores (no values)
    <out>/<set>__<detector>.json         evaluate() report, timing, detector info
and for ladder sets a ladder table (ladder_tables, plus neutral_tables with
base-resampled 95% CIs for ladder_neutral).

Detectors: gitleaks (defaults, max-decode-depth 5), gitleaks_d0
(--max-decode-depth 0) and presidio, reported as "Presidio + UK NINO" (stock
registry plus UkNinoRecognizer).
Values are never printed or written.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from castellan.analysis import items, ladder_tables, neutral_tables
from castellan.data.schema import read_jsonl
from castellan.evaluate import DocPrediction, evaluate, timed_predict
from castellan.spans import Finding

SETS = ["dev", "secrets_eval", "hardneg", "longctx", "final", "ladder", "ladder_neutral"]


def make_detectors(names):
    out = []
    for n in names:
        if n == "gitleaks":
            from castellan.baselines.gitleaks import GitleaksDetector

            out.append(GitleaksDetector())
        elif n == "gitleaks_d0":
            from castellan.baselines.gitleaks import GitleaksDetector

            d = GitleaksDetector(extra_args=["--max-decode-depth", "0"])
            d.name = "gitleaks_d0"
            out.append(d)
        elif n == "presidio":
            from castellan.baselines.presidio import PresidioDetector

            out.append(PresidioDetector())
        else:
            raise SystemExit(f"unknown detector {n}")
    return out


def save_preds(path: Path, preds) -> None:
    path.write_text(json.dumps([{"id": p.id, "findings": [f.to_dict() for f in p.findings],
                                 "doc_scores": p.doc_scores} for p in preds]))


def load_preds(path: Path) -> list[DocPrediction]:
    return [DocPrediction(d["id"], [Finding(f["start"], f["end"], f["label"], f["score"]) for f in d["findings"]],
                          d.get("doc_scores", {})) for d in json.loads(path.read_text())]


def git_sha() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runs/baselines")
    ap.add_argument("--sets", default=",".join(SETS))
    ap.add_argument("--detectors", default="gitleaks,gitleaks_d0,presidio")
    ap.add_argument("--eval-dir", default="eval_data")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "preds").mkdir(parents=True, exist_ok=True)
    dets = make_detectors(args.detectors.split(","))
    for s in args.sets.split(","):
        exs = list(read_jsonl(Path(args.eval_dir) / f"{s}.jsonl"))
        for d in dets:
            t0 = time.time()
            t = timed_predict(d.predict, exs)
            save_preds(out / "preds" / f"{s}__{d.name}.json", t.preds)
            info = {"detector": d.name, "display_name": getattr(d, "display_name", d.name), "version": getattr(d, "version", None), "git_sha": git_sha(),
                    "seconds": t.seconds, "ms_per_1k_tokens_approx": t.ms_per_1k_tokens}
            if hasattr(d, "recognizer_config"):
                info["recognizers"] = d.recognizer_config()
            if d.name.startswith("gitleaks"):
                info["args"] = d.extra_args
            rep = {"info": info, "metrics": evaluate(exs, t.preds)}
            if s.startswith("ladder"):
                its = items(exs, t.preds)
                rep["ladder"] = ladder_tables(its)
                if s == "ladder_neutral":
                    rep["neutral"] = neutral_tables(its)
            (out / f"{s}__{d.name}.json").write_text(json.dumps(rep, indent=1, default=float))
            print(json.dumps({"set": s, "detector": d.name, "n": len(exs), "seconds": round(time.time() - t0, 1)}),
                  flush=True)


if __name__ == "__main__":
    main()
