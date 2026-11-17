"""Build lookalike training sets from castellan.data.lookalikes without editing it.

    python scripts/build_lookalikes.py --n 5000 --rfc-jwt-frac 0.01 --out data_out/lk_n5000_jwtcap.jsonl
    python scripts/build_lookalikes.py --n 5000 --drop-category doc_example --out data_out/lk_n5000_nodoc.jsonl

Draws documents in generator order from a larger pool and skips the ones a
filter rejects, until N are kept. Prints counts only.
"""

import argparse
import json
from collections import Counter

from castellan.data.lookalikes import generate_lookalikes
from castellan.data.schema import write_jsonl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rfc-jwt-frac", type=float, default=None, help="cap docs holding the RFC 7519 JWT at this fraction")
    ap.add_argument("--drop-category", action="append", default=[])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cap = None if a.rfc_jwt_frac is None else int(round(a.rfc_jwt_frac * a.n))
    pool = generate_lookalikes(3 * a.n, seed=a.seed)
    kept, n_rfc, dropped = [], 0, Counter()
    for x in pool:
        if len(kept) == a.n:
            break
        if x.meta["category"] in a.drop_category:
            dropped["category:" + x.meta["category"]] += 1
            continue
        if "rfc7519_jwt" in x.meta["forms"]:
            if cap is not None and n_rfc >= cap:
                dropped["rfc_jwt_cap"] += 1
                continue
            n_rfc += 1
        kept.append(x)
    if len(kept) < a.n:
        raise SystemExit("pool too small")
    write_jsonl(a.out, kept)
    print(json.dumps({"out": a.out, "kept": len(kept), "rfc_jwt_docs": n_rfc, "dropped": dict(dropped),
                      "categories": dict(Counter(x.meta["category"] for x in kept))}, indent=1))


if __name__ == "__main__":
    main()
