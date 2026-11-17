"""M3 follow-up analysis (user, 2026-11-17). Reporting only: no retraining, no threshold changes to any
bundle. Thresholds computed here are for reporting and are never fed back into a detector.

    python scripts/m3_analysis.py q06 q17 q4 q8

1. Matched FPR per rung and size: probe recall at the threshold giving at most 5% FPR on that rung's
   matched controls, and secret-vs-control AUROC. Item score = max stage-2 probability over the secret
   classes among all proposed candidates in the document (``doc_scores`` in the stored predictions;
   candidates below the frozen threshold are included, unproposed text scores 0). This is a document-level
   score: a secret item counts at threshold t if its score exceeds t, whether or not the top candidate
   overlaps the gold piece. The frozen-threshold doc-level flag rate is reported next to it for reference.
2. Conditional test on rung-0-conditioned bases: P(probe flags | model_can_read) vs
   P(probe flags | not model_can_read) per rung (frozen operating point, item hit = gold overlap).
   Cells with fewer than 10 items are reported with counts only.
3. Union detector: probe OR Gitleaks default; secret recall, control FPR, hard-negative FPR per rung.
4. Rung-3 confound check: secret recall vs control and hard-negative flag rate, split by variant.

CIs: 2000 bootstrap resamples of whole bases (seed 0); matched controls move with their secret base.
For matched FPR the threshold is re-chosen inside every resample. Counts and rates only.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from run_baselines import load_preds  # noqa: E402

from castellan.analysis import base_key, items  # noqa: E402
from castellan.data.schema import SECRET_LABELS, read_jsonl  # noqa: E402

SETS = ("ladder", "ladder_neutral")
N_BOOT = 2000
TARGET_FPR = 0.05
MIN_CELL = 10


def load(name: str, s: str) -> dict:
    exs = list(read_jsonl(f"eval_data/{s}.jsonl"))
    preds = load_preds(Path(f"runs/m3/{name}/preds/{s}__probe.json"))
    score = {p.id: max((p.doc_scores or {}).get(lab, 0.0) for lab in SECRET_LABELS) for p in preds}
    probe = {i.id: i for i in items(exs, preds)}
    gl = {i.id: i for i in items(exs, load_preds(Path(f"runs/baselines/preds/{s}__gitleaks.json")))}
    rd = {}
    for ln in Path(f"runs/m3/readability/{name}__{s}.jsonl").read_text().splitlines():
        r = json.loads(ln)
        rd[r["id"]] = r["model_can_read"]
    meta = {e.id: e.meta or {} for e in exs}
    rung_of = {e.id: e.rung for e in exs}
    readable = {base_key(meta[i]["base_id"]) for i, ok in rd.items() if ok and rung_of[i] == 0}
    rows = []
    for e in exs:
        m = meta[e.id]
        rows.append({"id": e.id, "rung": e.rung, "kind": m["kind"], "variant": m.get("variant"),
                     "bk": base_key(m.get("base_id")) if m["kind"] != "hardneg" else None,
                     "score": score[e.id], "hit": probe[e.id].hit_any, "flag": probe[e.id].flagged,
                     "gl_hit": gl[e.id].hit_any, "gl_flag": gl[e.id].flagged, "read": rd.get(e.id)})
    return {"rows": rows, "readable": readable}


def matched(sec: np.ndarray, ctl: np.ndarray) -> dict:
    """Recall at the threshold giving at most TARGET_FPR on controls (flag if score > t)."""
    c = np.sort(ctl)[::-1]
    k = int(np.floor(TARGET_FPR * len(c)))
    t = c[k] if k < len(c) else -1.0
    return {"thr": float(t), "recall": float((sec > t).mean()), "fpr": float((ctl > t).mean()),
            "auroc": float(roc_auc_score(np.r_[np.ones(len(sec)), np.zeros(len(ctl))], np.r_[sec, ctl]))
            if len(sec) and len(ctl) else None}


def boot(rows: list[dict], stat, rng: np.random.Generator) -> tuple[float | None, float | None]:
    by = defaultdict(list)
    for r in rows:
        by[r["bk"]].append(r)
    keys = list(by)
    vals = []
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(keys), len(keys))
        v = stat([r for j in pick for r in by[keys[j]]])
        if v is not None:
            vals.append(v)
    if not vals:
        return None, None
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def rate(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def r4(x):
    return None if x is None else round(x, 4)


def analyse(d: dict, cond: bool, rng: np.random.Generator) -> dict:
    keep = (lambda r: r["bk"] in d["readable"]) if cond else (lambda r: True)
    rows = [r for r in d["rows"] if r["kind"] != "hardneg" and keep(r)]
    hn = [r for r in d["rows"] if r["kind"] == "hardneg"]
    out = {}
    for rung in sorted({r["rung"] for r in rows}):
        rr = [r for r in rows if r["rung"] == rung]
        sec = [r for r in rr if r["kind"] == "secret"]
        ctl = [r for r in rr if r["kind"] == "control"]
        hr = [r for r in hn if r["rung"] == rung]
        res = {"n_secret": len(sec), "n_control": len(ctl), "n_hardneg": len(hr)}
        # 1. matched FPR
        if ctl:
            def mstat(key, s):
                se = np.array([r["score"] for r in s if r["kind"] == "secret"])
                ct = np.array([r["score"] for r in s if r["kind"] == "control"])
                if not len(se) or not len(ct):
                    return None
                if key == "auroc" and len(set(np.r_[np.ones(len(se)), np.zeros(len(ct))])) < 2:
                    return None
                return matched(se, ct)[key]
            m = matched(np.array([r["score"] for r in sec]), np.array([r["score"] for r in ctl]))
            res["matched_5pct"] = {
                "threshold": r4(m["thr"]), "achieved_control_fpr": r4(m["fpr"]),
                "recall": r4(m["recall"]), "recall_ci": [r4(x) for x in boot(rr, lambda s: mstat("recall", s), rng)],
                "auroc": r4(m["auroc"]), "auroc_ci": [r4(x) for x in boot(rr, lambda s: mstat("auroc", s), rng)],
                "frozen_doclevel_secret_flag": r4(rate(r["flag"] for r in sec)),
                "frozen_doclevel_control_flag": r4(rate(r["flag"] for r in ctl)),
                "secret_score_zero_frac": r4(rate(r["score"] == 0 for r in sec)),
            }
        # 2. conditional test (only meaningful on conditioned bases; computed either way)
        cells = {}
        for lab, want in (("can_read", True), ("cannot_read", False)):
            cs = [r for r in sec if r["read"] is want]
            cell = {"n": len(cs), "n_flagged": sum(r["hit"] for r in cs)}
            if len(cs) >= MIN_CELL:
                cell["p_flag"] = r4(rate(r["hit"] for r in cs))
                cell["ci"] = [r4(x) for x in boot(sec, lambda s, w=want: rate(r["hit"] for r in s if r["read"] is w), rng)]
            cells[lab] = cell
        if all("p_flag" in c for c in cells.values()):
            def diff(s):
                a = rate(r["hit"] for r in s if r["read"] is True)
                b = rate(r["hit"] for r in s if r["read"] is False)
                return None if a is None or b is None else a - b
            cells["diff"] = r4(cells["can_read"]["p_flag"] - cells["cannot_read"]["p_flag"])
            cells["diff_ci"] = [r4(x) for x in boot(sec, diff, rng)]
        res["conditional"] = cells
        # 3. union
        res["union"] = {
            "probe": {"recall": r4(rate(r["hit"] for r in sec)), "control_fpr": r4(rate(r["flag"] for r in ctl)),
                      "hardneg_fpr": r4(rate(r["flag"] for r in hr))},
            "gitleaks": {"recall": r4(rate(r["gl_hit"] for r in sec)), "control_fpr": r4(rate(r["gl_flag"] for r in ctl)),
                         "hardneg_fpr": r4(rate(r["gl_flag"] for r in hr))},
            "union": {"recall": r4(rate(r["hit"] or r["gl_hit"] for r in sec)),
                      "control_fpr": r4(rate(r["flag"] or r["gl_flag"] for r in ctl)),
                      "hardneg_fpr": r4(rate(r["flag"] or r["gl_flag"] for r in hr))},
        }
        res["union"]["union"]["recall_ci"] = [r4(x) for x in boot(sec, lambda s: rate(r["hit"] or r["gl_hit"] for r in s), rng)]
        # 4. rung-3 confound check by variant (hard negatives are never conditioned)
        if rung == 3:
            conf = {}
            for v in ["all"] + sorted({r["variant"] for r in rr + hr if r["variant"]}):
                f = (lambda r: True) if v == "all" else (lambda r, v=v: r["variant"] == v)
                s_, c_, h_ = [r for r in sec if f(r)], [r for r in ctl if f(r)], [r for r in hr if f(r)]
                conf[v] = {"secret_recall": r4(rate(r["hit"] for r in s_)), "n_secret": len(s_),
                           "can_read": r4(rate(r["read"] for r in s_ if r["read"] is not None)),
                           "control_flag": r4(rate(r["flag"] for r in c_)), "n_control": len(c_),
                           "hardneg_flag": r4(rate(r["flag"] for r in h_)), "n_hardneg": len(h_)}
            res["rung3_confound"] = conf
        out[f"rung{rung}"] = res
    return out


def main() -> None:
    names = sys.argv[1:]
    rep = {}
    for n in names:
        rep[n] = {}
        for s in SETS:
            d = load(n, s)
            rng = np.random.default_rng(0)
            rep[n][s] = {"primary_conditioned": analyse(d, True, rng),
                         "secondary_unconditioned": analyse(d, False, rng)}
            print(json.dumps({"model": n, "set": s, "done": True}), flush=True)
    Path("runs/m3/analysis.json").write_text(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
