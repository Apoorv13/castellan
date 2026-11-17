"""M3 headline plot: probe recall vs model readability by rung, one panel per model size.

    python scripts/m3_plot.py q06 q17 q4 q8 --out docs/m3_headline.png

Data: ladder, primary analysis (each size on the bases it reads at rung 0), from runs/m3/report.json and
runs/m3/analysis.json. Gitleaks (defaults) on the same bases is drawn as a gray dashed reference line.
Probe recall at 5% matched-control FPR exists only for rungs that have controls (2 to 5). Rates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

LABEL = {"q06": "Qwen3-0.6B", "q17": "Qwen3-1.7B", "q4": "Qwen3-4B", "q8": "Qwen3-8B"}
RUNGS = ["0\nplain", "1\nsplit", "2\nbase64", "3\nhomoglyph", "4\nspelled", "5\nturns"]
BLUE, ORANGE, AQUA, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#8a8980"
INK, MUTED, GRID, SURFACE = "#1f1f1e", "#5f5e58", "#e6e5e0", "#fcfcfb"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="+")
    ap.add_argument("--out", default="docs/m3_headline.png")
    a = ap.parse_args()
    rep = json.loads(Path("runs/m3/report.json").read_text())
    ana = json.loads(Path("runs/m3/analysis.json").read_text())
    fig, axes = plt.subplots(1, len(a.names), figsize=(4.0 * len(a.names), 4.2), sharey=True, facecolor=SURFACE)
    x = list(range(6))
    for ax, n in zip(axes, a.names):
        t = rep[n]["ladder"]["primary_conditioned"]
        g = [t[f"rung{r}:all"]["gitleaks_recall"] for r in x]
        p = [t[f"rung{r}:all"]["probe_recall"] for r in x]
        c = [t[f"rung{r}:all"]["can_read"] for r in x]
        m = ana[n]["ladder"]["primary_conditioned"]
        mx = [r for r in x if "matched_5pct" in m.get(f"rung{r}", {})]
        my = [m[f"rung{r}"]["matched_5pct"]["recall"] for r in mx]
        ax.set_facecolor(SURFACE)
        ax.plot(x, g, color=GRAY, lw=1.5, ls=(0, (4, 3)), marker=None, label="Gitleaks (defaults)", zorder=2)
        ax.plot(x, c, color=ORANGE, lw=2, marker="^", ms=8, mec=SURFACE, mew=2, label="Model can read (rung 0 = 1 by conditioning)", zorder=3)
        ax.plot(x, p, color=BLUE, lw=2, marker="o", ms=8, mec=SURFACE, mew=2,
                label="Probe recall (frozen threshold)", zorder=4)
        ax.plot(mx, my, color=AQUA, lw=2, marker="s", ms=8, mec=SURFACE, mew=2,
                label="Probe recall at 5% control FPR (doc-level score)", zorder=4)
        nb = t["rung0:all"]["n_bases"]
        ax.set_title(f"{LABEL[n]}  ({nb} bases)", color=INK, fontsize=11, loc="left")
        ax.set_xticks(x, RUNGS, fontsize=8, color=MUTED)
        ax.set_ylim(-0.03, 1.03)
        ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.tick_params(colors=MUTED, length=0)
        ax.grid(axis="y", color=GRID, lw=1)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
    axes[0].set_ylabel("Rate (secret items)", color=MUTED)
    h, lab = axes[0].get_legend_handles_labels()
    order = [2, 3, 1, 0]
    fig.legend([h[i] for i in order], [lab[i] for i in order], loc="lower center", ncol=4, frameon=False,
               fontsize=9, labelcolor=INK)
    fig.suptitle("Probe recall vs model readability across the obfuscation ladder "
                 "(bases each model reads at rung 0)", color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0.08, 1, 0.95))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, dpi=150, facecolor=SURFACE)
    print(a.out)


if __name__ == "__main__":
    main()
