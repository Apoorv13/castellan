"""Counts-only summary of readability results, plus the 1.7B continue rule.

    python scripts/readability_summary.py q17 [--gate]

Per set, rung and announced/unannounced: n, model_can_read, empty, refusal,
think_emitted, truncated, and mean normalized edit distance. Rung 0 by family,
with every family below 0.95 flagged (that size's ladder report carries the
caveat). Continue rule (user, 2026-11-16), checked with --gate on 1.7B only:
rung-0 model_can_read >= 0.80 overall and zero refusals, else exit 1.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

from castellan.data.schema import read_jsonl

FLAGS = ("model_can_read", "empty", "refusal", "think_emitted", "truncated")


def main() -> None:
    name = sys.argv[1]
    gate = "--gate" in sys.argv
    out = {}
    r0 = defaultdict(int)
    fam = defaultdict(lambda: defaultdict(int))
    for s in ("ladder_neutral", "ladder"):
        f = Path(f"runs/m3/readability/{name}__{s}.jsonl")
        if not f.exists():
            continue
        meta = {e.id: (e.meta or {}) for e in read_jsonl(f"eval_data/{s}.jsonl")}
        tab = defaultdict(lambda: defaultdict(float))
        for ln in f.read_text().splitlines():
            r = json.loads(ln)
            a = meta[r["id"]].get("announced")
            key = f"rung{r['rung']}:" + ("announced" if a else "unannounced" if a is False else "n/a")
            for k in (f"rung{r['rung']}:all", key):
                tab[k]["n"] += 1
                for fl in FLAGS:
                    tab[k][fl] += bool(r[fl])
                if r.get("edit_dist_norm") is not None:
                    tab[k]["_ed_sum"] += r["edit_dist_norm"]
                    tab[k]["_ed_n"] += 1
            if r["rung"] == 0:
                for k in ("n", "model_can_read", "refusal", "think_emitted", "empty", "truncated"):
                    r0[k] += 1 if k == "n" else bool(r[k])
                fm = fam[meta[r["id"]].get("family")]
                fm["n"] += 1
                fm["read"] += r["model_can_read"]
        res_s = {}
        for k, v in sorted(tab.items()):
            d = {kk: int(vv) for kk, vv in v.items() if not kk.startswith("_")}
            if v["_ed_n"]:
                d["mean_edit_dist_norm"] = round(v["_ed_sum"] / v["_ed_n"], 4)
            res_s[k] = d
        out[s] = res_s
    res = {"model": name, "tables": out, "rung0": dict(r0)}
    if r0["n"]:
        for k in ("model_can_read", "refusal", "think_emitted"):
            res["rung0"][k + "_rate"] = r0[k] / r0["n"]
    res["rung0_by_family"] = {k: {"n": v["n"], "read": v["read"], "rate": round(v["read"] / v["n"], 3),
                                  "below_0.95": v["read"] / v["n"] < 0.95} for k, v in sorted(fam.items())}
    res["families_below_0.95"] = [k for k, v in res["rung0_by_family"].items() if v["below_0.95"]]
    ok = bool(r0["n"]) and res["rung0"]["model_can_read_rate"] >= 0.80 and r0["refusal"] == 0
    res["continue_rule"] = {"checked": gate, "passed": ok, "rule": "rung-0 can_read >= 0.80 and zero refusals"}
    Path(f"runs/m3/readability_summary_{name}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res["rung0"]), json.dumps(res["continue_rule"]),
          json.dumps({"families_below_0.95": res["families_below_0.95"]}))
    if gate and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
