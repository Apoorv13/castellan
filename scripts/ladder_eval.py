"""Run a probe bundle on ladder and ladder_neutral (measurement only, never tuned against).

    python scripts/ladder_eval.py --bundle bundles/probe-v1 --detector castellan.stage2:detector_factory \\
        --out runs/m3/ladder__q17

Writes <out>/<set>__probe.json (evaluate report, ladder tables, neutral tables
for ladder_neutral, timing) and <out>/preds/<set>__probe.json (offsets, labels,
scores only). Same reporting as scripts/run_baselines.py. Prints numbers only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_baselines import save_preds  # noqa: E402

from castellan.analysis import items, ladder_tables, neutral_tables  # noqa: E402
from castellan.bundle import ProbeDetector, load_bundle  # noqa: E402
from castellan.data.schema import read_jsonl  # noqa: E402
from castellan.evaluate import evaluate, timed_predict  # noqa: E402

SETS = ("ladder", "ladder_neutral")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--detector", default=None)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    b = load_bundle(a.bundle, device=a.device)
    if a.detector:
        mod, fn = a.detector.split(":")
        det = getattr(importlib.import_module(mod), fn)(b, a.bundle)
    else:
        det = ProbeDetector(b)
    out = Path(a.out)
    (out / "preds").mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for s in SETS:
        exs = list(read_jsonl(f"eval_data/{s}.jsonl"))
        t = timed_predict(det.predict, exs, lambda x: len(b.extractor.tokenize(x)))
        save_preds(out / "preds" / f"{s}__probe.json", t.preds)
        its = items(exs, t.preds)
        rep = {"info": {"git_sha": sha, "bundle": a.bundle, "detector": a.detector or "probe", "model_id": b.model_id,
                        "revision": b.revision, "dtype": b.dtype, "layer": b.layer,
                        "train_data_hash": b.meta.get("data_hash"), "seed": b.meta.get("seed")},
               "metrics": evaluate(exs, t.preds), "ladder": ladder_tables(its),
               "timing": {"ms_per_1k_tokens": t.ms_per_1k_tokens, "seconds": t.seconds,
                          "peak_rss_bytes": t.peak_rss_bytes, "mps_driver_bytes": t.mps_driver_bytes}}
        if s == "ladder_neutral":
            rep["neutral"] = neutral_tables(its)
        (out / f"{s}__probe.json").write_text(json.dumps(rep, indent=1, default=float))
        print(json.dumps({"set": s, "n": len(exs), "seconds": round(t.seconds, 1)}), flush=True)


if __name__ == "__main__":
    main()
