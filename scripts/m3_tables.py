"""Render runs/m3/report.json (plus per-size readability summaries and ladder char metrics) as markdown.

    python scripts/m3_tables.py q06 q17 q4 q8 > runs/m3/tables.md

Counts and rates only. Nothing here is tuned; the ladder is a measurement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LABEL = {"q06": "0.6B", "q17": "1.7B", "q4": "4B", "q8": "8B"}
MAIN = ("probe_recall", "control_fpr", "gitleaks_recall", "gitleaks_d0_recall", "presidio_recall", "can_read")
HEAD = ("Probe recall", "Control FPR", "Gitleaks", "Gitleaks d0", "Presidio+NINO", "Can read")


def f(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def ci(c):
    return f"{c['point']:.2f} [{c['lo']:.2f}, {c['hi']:.2f}]" if c and c.get("point") is not None else "n/a"


def per_model_table(t, extra=()):
    cols = HEAD + tuple(e[1] for e in extra)
    out = ["| Rung : split | Items (bases) | " + " | ".join(cols) + " |", "|" + " --- |" * (len(cols) + 2)]
    for k, v in t.items():
        vals = [f(v[c]) for c in MAIN] + [f(v[e[0]]) for e in extra]
        out.append(f"| {k} | {v['n_secret_items']} ({v['n_bases']}) | " + " | ".join(vals) + " |")
    return out


def main() -> None:
    names = sys.argv[1:]
    rep = json.loads(Path("runs/m3/report.json").read_text())
    L = []
    L += ["#### Readability at rung 0 (prompt v2, exact match, greedy, thinking off)", "",
          "| Model | Read / 284 | Rate | Refusals | Think blocks | Truncated | Families below 0.95 | Readable bases ladder / ladder_neutral |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for n in names:
        r0 = rep[n]["rung0"]
        L.append(f"| {LABEL[n]} | {r0['model_can_read']} | {f(r0['model_can_read_rate'])} | {r0['refusal']} | "
                 f"{r0['think_emitted']} | {r0['truncated']} | {len(rep[n]['families_below_0.95_at_rung0'])} of 40 | "
                 f"{rep[n]['ladder']['n_readable_bases']} of {rep[n]['ladder']['n_secret_bases']} / "
                 f"{rep[n]['ladder_neutral']['n_readable_bases']} of {rep[n]['ladder_neutral']['n_secret_bases']} |")
    L.append("")
    for n in names:
        L.append(f"- {LABEL[n]} families below 0.95: " + ", ".join(rep[n]["families_below_0.95_at_rung0"]))
    L.append("")

    L += ["#### Headline: ladder, all splits pooled, primary (each size on its own bases read at rung 0)", "",
          "Per size: probe item recall, Gitleaks default and depth-0 recall on the same bases, model can-read rate, "
          "control FPR. Rungs 0 and 1 have no controls.", "",
          "| Rung | Model | Bases | Probe | Gitleaks | Gitleaks d0 | Presidio+NINO | Can read | Control FPR |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in range(6):
        for n in names:
            v = rep[n]["ladder"]["primary_conditioned"][f"rung{r}:all"]
            L.append(f"| {r} | {LABEL[n]} | {v['n_bases']} | {f(v['probe_recall'])} | {f(v['gitleaks_recall'])} | "
                     f"{f(v['gitleaks_d0_recall'])} | {f(v['presidio_recall'])} | {f(v['can_read'])} | {f(v['control_fpr'])} |")
    L.append("")

    L += ["#### ladder_neutral headline, primary, 95% CIs resampling whole bases (2000 resamples)", "",
          "| Rung | " + " | ".join(f"{LABEL[n]} probe | {LABEL[n]} read | {LABEL[n]} ctrl FPR" for n in names) + " |",
          "|" + " --- |" * (3 * len(names) + 1)]
    rungs = sorted(rep[names[0]]["ladder_neutral"]["primary_cis"], key=lambda s: int(s[4:]))
    for rk in rungs:
        row = [f"| {rk[4:]} |"]
        for n in names:
            c = rep[n]["ladder_neutral"]["primary_cis"][rk]
            row.append(f" {ci(c['probe_recall'])} | {ci(c['can_read'])} | {ci(c['control_fpr'])} |")
        L.append("".join(row))
    L.append("")

    for s in ("ladder", "ladder_neutral"):
        for n in names:
            d = rep[n][s]
            L += [f"#### {s}, {LABEL[n]}, primary (conditioned on {d['n_readable_bases']} of {d['n_secret_bases']} bases read at rung 0)", ""]
            L += per_model_table(d["primary_conditioned"])
            if s == "ladder_neutral":
                L += ["", "Base-resampled 95% CIs (primary):", "",
                      "| Rung | Probe recall | Control FPR | Gitleaks | Gitleaks d0 | Presidio+NINO | Can read |",
                      "| --- | --- | --- | --- | --- | --- | --- |"]
                for rk in rungs:
                    c = d["primary_cis"][rk]
                    L.append(f"| {rk[4:]} | " + " | ".join(ci(c[m]) for m in MAIN) + " |")
            L.append("")

    L += ["#### Secondary: unconditioned (all bases), with mean normalized edit distance", ""]
    for s in ("ladder", "ladder_neutral"):
        for n in names:
            d = rep[n][s]
            hn = d["hardneg_fpr_unconditioned"]
            L += [f"{s}, {LABEL[n]}" + (f" (ladder hard-negative item flag rate {f(hn)})" if hn is not None else ""), ""]
            L += per_model_table(d["secondary_unconditioned"], extra=(("edit_dist", "Mean norm. edit dist"),))
            if s == "ladder_neutral":
                L += ["", "Base-resampled 95% CIs (unconditioned):", "",
                      "| Rung | Probe recall | Control FPR | Gitleaks | Gitleaks d0 | Presidio+NINO | Can read |",
                      "| --- | --- | --- | --- | --- | --- | --- |"]
                for rk in rungs:
                    c = d["secondary_cis"][rk]
                    L.append(f"| {rk[4:]} | " + " | ".join(ci(c[m]) for m in MAIN) + " |")
            L.append("")

    L += ["#### Secondary: ladder hard-negative item flag rate per rung (probe, any secret span)", "",
          "| Model | " + " | ".join(f"Rung {r}" for r in range(6)) + " |", "|" + " --- |" * 7]
    for n in names:
        br = json.loads(Path(f"runs/m3/{n}/ladder__probe.json").read_text())["ladder"]["by_rung"]
        vals = [f(br[str(r)]["hardneg"]["neg_flag_rate"]) + f" (n={br[str(r)]['hardneg']['n_neg']})"
                if str(r) in br and "hardneg" in br[str(r)] else "n/a" for r in range(6)]
        L.append(f"| {LABEL[n]} | " + " | ".join(vals) + " |")
    L.append("")

    L += ["#### Secondary: probe char metrics per rung (secret items, unconditioned)", "",
          "| Set | Model | " + " | ".join(f"Rung {r} char recall" for r in range(6)) + " | Secret char P (whole set) | char FPR any label |",
          "|" + " --- |" * 10]
    for s in ("ladder", "ladder_neutral"):
        for n in names:
            p = json.loads(Path(f"runs/m3/{n}/{s}__probe.json").read_text())
            br = p["ladder"]["by_rung"]
            cr = [f(br[str(r)]["secret"]["char_recall"]) if str(r) in br and "secret" in br[str(r)] else "n/a" for r in range(6)]
            sec = p["metrics"]["categories"]["secret"]
            L.append(f"| {s} | {LABEL[n]} | " + " | ".join(cr) + f" | {f(sec['char_precision'])} | {f(p['metrics']['any_label']['char_fpr'], 4)} |")
    L.append("")
    print("\n".join(L))


if __name__ == "__main__":
    main()
