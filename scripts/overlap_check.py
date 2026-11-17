"""Counts-only contamination check: lookalike training docs vs every frozen eval set.

    python scripts/overlap_check.py data_out/lk_n5000_jwtcap.jsonl data_out/lk_n5000_nodoc.jsonl

Allowed by the user on 2026-11-16. Prints counts only, never text.
A shared K-char substring is "fixture" when it lies inside a public example
value (castellan.data.lookalikes.DOC_EXAMPLE_VALUES: AWS docs example keys, the
gitleaks GCP allowlist key, the RFC 7519 example JWT); otherwise "other", split
by character mix so boilerplate prose can be told apart from value-like text.
"""

import hashlib
import json
import sys
from collections import Counter, defaultdict

from castellan.data.lookalikes import DOC_EXAMPLE_VALUES
from castellan.data.schema import read_jsonl

K = 40
EVAL = ["dev", "hardneg", "secrets_eval", "longctx", "final", "ladder", "ladder_neutral"]


def h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


# Standard public-key format constants (RFC 7468 PEM armor, RFC 8410 Ed25519
# SPKI prefix in base64, OpenSSH key-type headers in base64). Not values.
FORMAT_CONSTANTS = ("-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----", "MCowBQYDK2VwAyEA",
                    "AAAAC3NzaC1lZDI1NTE5", "AAAAB3NzaC1yc2E", "-----BEGIN CERTIFICATE-----",
                    "-----END CERTIFICATE-----")
FIX16 = {v[i:i + 16] for v in DOC_EXAMPLE_VALUES for i in range(len(v) - 15)}


def explain(s: str) -> str:
    if any(s[i:i + 16] in FIX16 for i in range(len(s) - 15)):
        return "fixture_adjacent"
    if any(c in s for c in FORMAT_CONSTANTS):
        return "format_constant"
    return "unexplained"


def mix(s: str) -> str:
    if any(c.isspace() for c in s):
        return "has_space"
    if s.isalnum():
        return "alnum_only"
    return "no_space_symbols"


def main() -> None:
    fixture = {h(v[i:i + K]) for v in DOC_EXAMPLE_VALUES for i in range(len(v) - K + 1)}
    sh = defaultdict(set)  # shingle hash -> categories
    kind = {}  # shingle hash -> fixture | other:<mix>
    exact = {}
    n_lk = 0
    for path in sys.argv[1:]:
        for e in read_jsonl(path):
            n_lk += 1
            cat = e.meta["category"]
            exact.setdefault(hashlib.sha256(e.text.encode()).hexdigest(), set()).add(cat)
            t = e.text
            for i in range(len(t) - K + 1):
                s = t[i:i + K]
                x = h(s)
                sh[x].add(cat)
                if x not in kind:
                    kind[x] = "fixture" if x in fixture else "other:" + explain(s) + ":" + mix(s)
    out = {"n_lookalike_docs": n_lk, "K": K, "sets": {}}
    for name in EVAL:
        docs = 0
        res = Counter()
        by_cat = Counter()
        for e in read_jsonl(f"eval_data/{name}.jsonl"):
            docs += 1
            if hashlib.sha256(e.text.encode()).hexdigest() in exact:
                res["exact_dupes"] += 1
            t = e.text
            kinds, cats = set(), set()
            n_other = 0
            for i in range(len(t) - K + 1):
                x = h(t[i:i + K])
                if x in sh:
                    k = kind[x]
                    kinds.add(k)
                    if k != "fixture":
                        n_other += 1
                        cats |= {c + ":" + k for c in sh[x]}
                    else:
                        cats |= {c + ":fixture" for c in sh[x]}
            for k in kinds:
                res["docs_sharing:" + k] += 1
            if n_other:
                res["docs_sharing_any_other"] += 1
                res["other_shingle_positions_total"] += n_other
            for c in cats:
                by_cat[c] += 1
        out["sets"][name] = {"n_docs": docs, **dict(sorted(res.items())), "docs_by_lookalike_category": dict(sorted(by_cat.items()))}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
