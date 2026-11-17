"""M3 headline tables: rung x model size (measurement only; nothing here is tuned).

    python scripts/m3_report.py q06 q17 q4 q8

Primary (user, 2026-11-16): per model, restrict to bases the model reads
correctly at rung 0 (readability v2, exact match). Per rung, split by
announced / unannounced:
  - probe item recall (any gold piece hit by a secret-label finding)
  - control FPR (matched controls of those bases flagged by the probe)
  - Gitleaks (defaults, depth 0) item recall, secret labels
  - Presidio + UK NINO item recall: a finding of ANY label overlapping a gold
    piece counts, because Presidio has no secret labels
  - model_can_read rate at that rung
ladder_neutral gets bootstrap 95% CIs resampling whole bases (controls move
with their matched secret base).
Secondary: the same without conditioning, plus mean normalized edit distance
and hard-negative FPR (hard negatives are not paired with secret bases, so
they are never conditioned).
Counts and rates only.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_baselines import load_preds  # noqa: E402

from castellan.analysis import base_key, bootstrap_ci, items  # noqa: E402
from castellan.data.schema import LABELS, read_jsonl  # noqa: E402

SETS = ("ladder", "ladder_neutral")
DETS = ("gitleaks", "gitleaks_d0", "presidio")


def rate(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def load(name: str, s: str):
    exs = list(read_jsonl(f"eval_data/{s}.jsonl"))
    meta = {e.id: e.meta or {} for e in exs}
    its = {"probe": {i.id: i for i in items(exs, load_preds(Path(f"runs/m3/{name}/preds/{s}__probe.json")))}}
    for d in DETS:
        labs = LABELS if d == "presidio" else None
        p = load_preds(Path(f"runs/baselines/preds/{s}__{d}.json"))
        its[d] = {i.id: i for i in (items(exs, p, labs) if labs else items(exs, p))}
    rd = {}
    f = Path(f"runs/m3/readability/{name}__{s}.jsonl")
    for ln in f.read_text().splitlines():
        r = json.loads(ln)
        rd[r["id"]] = r
    readable = {base_key(meta[i]["base_id"]) for i, r in rd.items() if r["rung"] == 0 and r["model_can_read"]}
    return exs, meta, its, rd, readable


def rows(exs, meta, its, rd, keep):
    """Per rung and announced split: item lists for each statistic."""
    out = defaultdict(lambda: defaultdict(list))
    for e in exs:
        m = meta[e.id]
        if m["kind"] == "hardneg":
            continue
        bk = base_key(m["base_id"])
        if not keep(bk):
            continue
        ann = m.get("announced")
        for split in ("all", "announced" if ann else "unannounced" if ann is False else None):
            if split is None:
                continue
            k = (e.rung, split)
            if m["kind"] == "secret":
                out[k]["probe_recall"].append(its["probe"][e.id].hit_any)
                for d in DETS:
                    out[k][f"{d}_recall"].append(its[d][e.id].hit_any)
                if e.id in rd:
                    out[k]["can_read"].append(rd[e.id]["model_can_read"])
                    if rd[e.id].get("edit_dist_norm") is not None:
                        out[k]["edit_dist"].append(rd[e.id]["edit_dist_norm"])
                out[k]["_bases"].append(bk)
            elif m["kind"] == "control":
                out[k]["control_fpr"].append(its["probe"][e.id].flagged)
    return out


def table(out):
    t = {}
    for (rung, split), v in sorted(out.items(), key=lambda x: (x[0][0], x[0][1])):
        t[f"rung{rung}:{split}"] = {"n_secret_items": len(v["probe_recall"]), "n_bases": len(set(v["_bases"])),
                                    "n_controls": len(v["control_fpr"]),
                                    **{k: (round(rate(v[k]), 4) if v[k] else None)
                                       for k in ("probe_recall", "control_fpr", "gitleaks_recall", "gitleaks_d0_recall",
                                                 "presidio_recall", "can_read", "edit_dist")}}
    return t


def neutral_cis(exs, meta, its, rd, keep):
    """ladder_neutral: base-resampled 95% CIs per rung for probe recall, control FPR and can_read."""
    probe = its["probe"]
    res = {}
    for r in sorted({e.rung for e in exs}):
        sel = [probe[e.id] for e in exs if e.rung == r and keep(base_key(meta[e.id]["base_id"]))
               and meta[e.id]["kind"] in ("secret", "control")]
        cr = {e.id: rd[e.id]["model_can_read"] for e in exs if e.id in rd}
        res[f"rung{r}"] = {
            "probe_recall": bootstrap_ci(sel, lambda s: rate(i.hit_any for i in s if i.kind == "secret")),
            "control_fpr": bootstrap_ci(sel, lambda s: rate(i.flagged for i in s if i.kind == "control")),
            "can_read": bootstrap_ci(sel, lambda s: rate(cr[i.id] for i in s if i.kind == "secret" and i.id in cr)),
        }
        for d in DETS:
            dsel = [its[d][i.id] for i in sel]
            res[f"rung{r}"][f"{d}_recall"] = bootstrap_ci(dsel, lambda s: rate(i.hit_any for i in s if i.kind == "secret"))
    return res


def main() -> None:
    names = sys.argv[1:]
    report = {}
    for name in names:
        summ = json.loads(Path(f"runs/m3/readability_summary_{name}.json").read_text())
        rep = {"families_below_0.95_at_rung0": summ["families_below_0.95"], "rung0": summ["rung0"]}
        for s in SETS:
            exs, meta, its, rd, readable = load(name, s)
            n_bases = len({base_key(meta[e.id]["base_id"]) for e in exs if meta[e.id]["kind"] == "secret"})
            cond = lambda bk, R=readable: bk in R  # noqa: E731
            allb = lambda bk: True  # noqa: E731
            hn = [its["probe"][e.id].flagged for e in exs if meta[e.id]["kind"] == "hardneg"]
            rep[s] = {
                "n_secret_bases": n_bases, "n_readable_bases": len(readable),
                "primary_conditioned": table(rows(exs, meta, its, rd, cond)),
                "secondary_unconditioned": table(rows(exs, meta, its, rd, allb)),
                "hardneg_fpr_unconditioned": round(rate(hn), 4) if hn else None,
            }
            if s == "ladder_neutral":
                rep[s]["primary_cis"] = neutral_cis(exs, meta, its, rd, cond)
                rep[s]["secondary_cis"] = neutral_cis(exs, meta, its, rd, allb)
        report[name] = rep
        print(json.dumps({"model": name, "readable_bases": {s: report[name][s]["n_readable_bases"] for s in SETS},
                          "families_below_0.95": rep["families_below_0.95_at_rung0"]}))
    Path("runs/m3/report.json").write_text(json.dumps(report, indent=1, default=float))


if __name__ == "__main__":
    main()
