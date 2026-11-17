"""Render runs/m3/analysis.json as markdown (counts and rates only).

    python scripts/m3_analysis_tables.py q06 q17 q4 q8 > runs/m3/analysis_tables.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LABEL = {"q06": "0.6B", "q17": "1.7B", "q4": "4B", "q8": "8B"}


def f(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def ci(p, c):
    return "n/a" if p is None else (f"{p:.2f} [{c[0]:.2f}, {c[1]:.2f}]" if c and c[0] is not None else f"{p:.2f}")


def rk(d):
    return sorted(d, key=lambda k: int(k[4:]))


def main() -> None:
    names = sys.argv[1:]
    a = json.loads(Path("runs/m3/analysis.json").read_text())
    L = []
    for s in ("ladder", "ladder_neutral"):
        L += [f"#### 1. Matched 5% control FPR, {s} (primary: bases read at rung 0)", "",
              "| Size | Rung | Secrets / controls | Threshold (reporting only) | Achieved control FPR | Recall [95% CI] | AUROC secret vs control [95% CI] | Frozen doc-level flag: secrets / controls | Secrets with no candidate |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for n in names:
            d = a[n][s]["primary_conditioned"]
            for k in rk(d):
                m = d[k].get("matched_5pct")
                if not m:
                    continue
                L.append(f"| {LABEL[n]} | {k[4:]} | {d[k]['n_secret']} / {d[k]['n_control']} | {f(m['threshold'], 4)} | "
                         f"{f(m['achieved_control_fpr'])} | {ci(m['recall'], m['recall_ci'])} | {ci(m['auroc'], m['auroc_ci'])} | "
                         f"{f(m['frozen_doclevel_secret_flag'])} / {f(m['frozen_doclevel_control_flag'])} | {f(m['secret_score_zero_frac'])} |")
        L.append("")
    for s in ("ladder", "ladder_neutral"):
        L += [f"#### 2. Conditional test, {s} (bases read at rung 0; frozen operating point; cells under 10 items show counts only)", "",
              "| Size | Rung | Can read: flagged / n | P(flag given can read) [CI] | Cannot read: flagged / n | P(flag given cannot read) [CI] | Difference [CI] |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for n in names:
            d = a[n][s]["primary_conditioned"]
            for k in rk(d):
                c = d[k]["conditional"]
                cr, nr = c["can_read"], c["cannot_read"]
                L.append(f"| {LABEL[n]} | {k[4:]} | {cr['n_flagged']} / {cr['n']} | {ci(cr.get('p_flag'), cr.get('ci'))} | "
                         f"{nr['n_flagged']} / {nr['n']} | {ci(nr.get('p_flag'), nr.get('ci'))} | "
                         f"{ci(c.get('diff'), c.get('diff_ci'))} |")
        L.append("")
    for s in ("ladder", "ladder_neutral"):
        L += [f"#### 3. Union detector (probe OR Gitleaks defaults), {s} (primary; hard negatives are never conditioned)", "",
              "| Size | Rung | Probe recall | Gitleaks recall | Union recall [CI] | Control FPR probe / Gitleaks / union | Hard-negative FPR probe / Gitleaks / union |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for n in names:
            d = a[n][s]["primary_conditioned"]
            for k in rk(d):
                u = d[k]["union"]
                L.append(f"| {LABEL[n]} | {k[4:]} | {f(u['probe']['recall'])} | {f(u['gitleaks']['recall'])} | "
                         f"{ci(u['union']['recall'], u['union']['recall_ci'])} | "
                         f"{f(u['probe']['control_fpr'])} / {f(u['gitleaks']['control_fpr'])} / {f(u['union']['control_fpr'])} | "
                         f"{f(u['probe']['hardneg_fpr'])} / {f(u['gitleaks']['hardneg_fpr'])} / {f(u['union']['hardneg_fpr'])} |")
        L.append("")
    L += ["#### 4. Rung-3 confound check (all bases, unconditioned; probe at its frozen operating point)", "",
          "| Set | Size | Variant | Secret recall (n) | Can read | Control flag rate (n) | Hard-negative flag rate (n) |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for s in ("ladder", "ladder_neutral"):
        for n in names:
            c = a[n][s]["secondary_unconditioned"]["rung3"]["rung3_confound"]
            for v, x in c.items():
                L.append(f"| {s} | {LABEL[n]} | {v} | {f(x['secret_recall'])} ({x['n_secret']}) | {f(x['can_read'])} | "
                         f"{f(x['control_flag'])} ({x['n_control']}) | "
                         + (f"{f(x['hardneg_flag'])} ({x['n_hardneg']})" if x['n_hardneg'] else "n/a") + " |")
    L.append("")
    print("\n".join(L))


if __name__ == "__main__":
    main()
