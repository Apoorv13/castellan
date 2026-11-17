"""Real benign negatives from a permissive code corpus, mined with the probe.

    python scripts/mine_negatives.py prepare --out data_out/mined/pool.jsonl
    python scripts/mine_negatives.py mine --bundle bundles/q17_s0 --pool data_out/mined/pool.jsonl \
        --skip data_out/mined/r1.jsonl --n-score 8000 --top 1500 --out data_out/mined/r1.jsonl

Sources (user decision 2026-11-16, see EXPERIMENTS.md "Dataset licenses"):
- codeparrot/github-code-clean parquet shards, keeping only files whose
  per-file license is MIT, Apache-2.0, BSD-2/3, ISC, CC0 or Unlicense.
- YAML/JSON/TOML/INI/.cfg files from permissively licensed packages in the
  venv.

Contamination guard. Mining picks exactly what a regex scanner misses, and
in real code that includes real secrets. A chunk becomes a training negative
only if it survives every rule below. When in doubt, drop.
  (a) gitleaks (default rules, --redact, report to file) flags its file
  (b) a credential-like key name next to a non-trivial value
  (c) URL userinfo (scheme://user:pass@)
  (d) JWT shape, PEM private block, or a known vendor prefix
  (e) a high-entropy value of 20+ chars with no hash/ID/digest context
  (f) PII-like content (email, IPv4, copyright or author lines), since real
      PII must not be taught as negative
Rules (c), (d) and (f) run on every chunk. Rules (b) and (e) run on every
probe candidate and on the rest of the chunk. Only counts per reason are
written; values are never printed or logged.

Training code never reads eval_data/, and neither does this script.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.metadata as md
import json
import math
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

from castellan.data.schema import Example, read_jsonl, write_jsonl

# Local data root (corpora/, data_out/); defaults to the repo root.
ROOT = Path(os.environ.get("CASTELLAN_ROOT", Path(__file__).resolve().parents[1]))
CORPUS_DIR = ROOT / "corpora" / "github-code-clean"
PERMISSIVE = {"mit", "apache-2.0", "bsd-3-clause", "bsd-2-clause", "isc", "cc0-1.0", "unlicense"}
VENV_PERM = ("mit", "apache", "bsd", "isc", "psf", "python software foundation", "cc0", "unlicense", "0bsd", "zlib")
CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
EXT = {"Python": "py", "JavaScript": "js", "TypeScript": "ts", "Shell": "sh", "Dockerfile": "Dockerfile",
       "Makefile": "mk", "GO": "go", "Java": "java", "Ruby": "rb", "PHP": "php", "C": "c", "C++": "cpp",
       "C#": "cs", "Markdown": "md", "HTML": "html", "CSS": "css", "SQL": "sql", "Rust": "rs",
       "PowerShell": "ps1", "Batchfile": "bat", "CMake": "cmake", "Scala": "scala", "Lua": "lua", "Perl": "pl"}

CHUNK_CHARS = 1200
MIN_CHUNK = 200
MAX_CHUNKS_PER_FILE = 3

# (c) URL userinfo
RE_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@'\"<>]+:[^\s/@'\"<>]*@")
# (d) JWT, PEM private block, vendor prefixes
RE_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\.")
RE_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")
RE_VENDOR = re.compile(
    r"(?<![A-Za-z0-9])(AKIA|ASIA|sk-|sk_live|rk_live|ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|xox[abporsc]-|AIza|hf_|"
    r"r8_|pplx-|gsk_|LTAI|dop_v1_)[A-Za-z0-9_\-]{4,}")
# (f) PII-like content
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
RE_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
RE_HEADER = re.compile(r"(?i)copyright|\bauthor|@author|maintainer")
# (b) credential-like key names
KEYWORDS = r"(password|passwd|pwd|secret|token|key|auth|credential|private|session)"
RE_KEYNAME = re.compile(r"(?i)[A-Za-z0-9_.\-]*" + KEYWORDS + r"[A-Za-z0-9_.\-]*")
RE_KEY_ASSIGN = re.compile(
    r"(?i)[A-Za-z0-9_.\-]*" + KEYWORDS + r"[A-Za-z0-9_.\-]*['\"]?\s*(?:=>|[:=])\s*"
    r"(?:['\"]([^'\"\n]{8,})['\"]|([A-Za-z0-9+/=_\-.~]{8,})\s*$)", re.M)
# (e) high entropy
RE_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
HASH_CONTEXT = re.compile(r"(?i)hash|sha\d*|md5|digest|checksum|integrity|uuid|guid|\bid\b|_id\b|commit|\brev|"
                          r"revision|etag|fingerprint|crc|version|\bver\b")


def entropy(s: str) -> float:
    c = collections.Counter(s)
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def high_entropy(tok: str) -> bool:
    return (len(tok) >= 20 and any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok)
            and entropy(tok) >= 3.0)


def line_context(text: str, a: int, b: int) -> str:
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    return text[ls: le if le >= 0 else len(text)]


def chunk_reasons(text: str) -> list[str]:
    """Rules (c), (d), (f) on the whole chunk, plus (b) and (e) on its literals."""
    r = []
    if RE_USERINFO.search(text):
        r.append("c_userinfo")
    if RE_JWT.search(text) or RE_PEM.search(text) or RE_VENDOR.search(text):
        r.append("d_jwt_pem_vendor")
    if RE_EMAIL.search(text) or RE_IPV4.search(text) or RE_HEADER.search(text):
        r.append("f_pii_like")
    if RE_KEY_ASSIGN.search(text):
        r.append("b_keyname_value")
    for m in RE_TOKEN.finditer(text):
        if high_entropy(m.group()) and not HASH_CONTEXT.search(line_context(text, m.start(), m.end())):
            r.append("e_high_entropy")
            break
    return r


def candidate_reasons(text: str, a: int, b: int) -> list[str]:
    """Rules (b) to (e) on one probe candidate span."""
    val = text[a:b]
    line = line_context(text, a, b)
    left = text[max(0, a - 60):a].split("\n")[-1]
    r = []
    if len(val.strip()) >= 6 and RE_KEYNAME.search(left) and re.search(KEYWORDS, left, re.I):
        r.append("b_keyname_value")
    if RE_USERINFO.search(line):
        r.append("c_userinfo")
    if RE_JWT.search(val) or RE_PEM.search(line) or RE_VENDOR.search(val):
        r.append("d_jwt_pem_vendor")
    for m in RE_TOKEN.finditer(val):
        if high_entropy(m.group()) and not HASH_CONTEXT.search(line):
            r.append("e_high_entropy")
            break
    if not HASH_CONTEXT.search(line):
        v = val.strip().strip("'\"`")
        if len(v) >= 16 and shape(v) == "secret_shaped":
            r.append("g_long_secret_shaped")
        elif quoted_literal_len(text, a, b) >= 12:
            r.append("g_quoted_literal")
    return r


def quoted_literal_len(text: str, a: int, b: int) -> int:
    """Length of the quoted string literal on the same line that contains [a, b), or 0."""
    line_start = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    line_end = le if le >= 0 else len(text)
    for q in "\"'`":
        i = text.rfind(q, line_start, a + 1)
        if i < 0:
            continue
        j = text.find(q, max(b - 1, i + 1), line_end)
        if j > i and j >= b - 1:
            return j - i - 1
    return 0


def shape(v: str) -> str:
    """ident: letters and underscore only. code_fragment: whitespace, brackets or newline.
    secret_shaped: one token with letters plus digits or base64 symbols (+ / =). other: the rest
    (for example dotted or dashed names without digits)."""
    if re.fullmatch(r"[A-Za-z_]+", v):
        return "ident"
    if re.search(r"[\s()\[\]{}]", v):
        return "code_fragment"
    if any(ch.isalpha() for ch in v) and (any(ch.isdigit() for ch in v) or re.search(r"[+/=]", v)):
        return "secret_shaped"
    return "other"


def bucket(val: str) -> str:
    v = val.strip().strip("'\"`")
    n = len(v)
    lb = "lt8" if n < 8 else ("8to15" if n < 16 else "16plus")
    return f"{lb}:{shape(v)}"


def split_chunks(text: str, r: random.Random) -> list[str]:
    lines = text.splitlines(keepends=True)
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = ""
        cur += ln[: CHUNK_CHARS * 2]
    if cur:
        chunks.append(cur)
    chunks = [c for c in chunks if len(c.strip()) >= MIN_CHUNK]
    r.shuffle(chunks)
    return chunks[:MAX_CHUNKS_PER_FILE]


def venv_configs() -> list[tuple[str, str, str]]:
    out = []
    for d in md.distributions():
        meta = d.metadata
        lic = " ".join(filter(None, [meta.get("License-Expression") or "", (meta.get("License") or "")[:200]]
                              + [c for c in (meta.get_all("Classifier") or []) if c.startswith("License")])).lower()
        if not any(p in lic for p in VENV_PERM) or "gpl" in lic:
            continue
        for f in d.files or []:
            if Path(str(f)).suffix in CONFIG_EXT:
                p = Path(d.locate_file(f))
                if p.is_file() and p.stat().st_size < 200_000:
                    try:
                        out.append((meta["Name"], str(f), p.read_text(encoding="utf-8")))
                    except UnicodeDecodeError:
                        pass
    return out


def gitleaks_flagged(files: dict[str, str], work: Path) -> set[str]:
    """Write files to a staging dir, run gitleaks with --redact, return flagged keys. Report stays on disk."""
    stage = work / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for k, text in files.items():
        (stage / k).write_text(text, encoding="utf-8")
    report = work / "gitleaks_report.json"
    subprocess.run(["gitleaks", "dir", str(stage), "--report-format", "json", "--report-path", str(report),
                    "--redact", "--no-banner", "--exit-code", "0", "--log-level", "error"],
                   check=True, capture_output=True)
    flagged = {Path(f["File"]).name for f in json.loads(report.read_text() or "[]")}
    shutil.rmtree(stage)
    report.unlink()
    return flagged


def cmd_prepare(a) -> None:
    import pyarrow.parquet as pq

    r = random.Random(a.seed)
    rows = []
    for shard in sorted(CORPUS_DIR.glob("train-*.parquet"))[: a.shards]:
        t = pq.read_table(shard, columns=["code", "language", "license", "size", "repo_name"]).to_pylist()
        rows.extend(x for x in t if x["license"] in PERMISSIVE and x["size"] < 200_000)
    n_perm = len(rows)
    r.shuffle(rows)
    rows = rows[: a.n_files]
    files = {}
    meta = {}
    for i, x in enumerate(rows):
        k = f"gcc{i:06d}.{EXT.get(x['language'], 'txt')}"
        files[k] = x["code"]
        meta[k] = ("real:codeparrot/github-code-clean", f"real_code:{x['language']}", x["license"])
    for i, (dist, rel, text) in enumerate(venv_configs()):
        k = f"venv{i:04d}{Path(rel).suffix}"
        files[k] = text
        meta[k] = ("real:venv-packages", f"real_config:{Path(rel).suffix[1:]}", dist)
    counts = collections.Counter()
    counts["files_permissive_in_shards"] = n_perm
    counts["files_sampled"] = len(files)
    flagged = gitleaks_flagged(files, Path(a.out).parent / "gl_work")
    counts["files_dropped_a_gitleaks"] = len(flagged)
    out = []
    for k, text in files.items():
        if k in flagged:
            continue
        src, carrier, lic = meta[k]
        for j, ch in enumerate(split_chunks(text, r)):
            counts["chunks_total"] += 1
            reasons = chunk_reasons(ch)
            for reason in reasons:
                counts[f"chunk_reason:{reason}"] += 1
            if reasons:
                counts["chunks_dropped"] += 1
                continue
            cid = hashlib.sha256(f"{k}:{j}:{ch}".encode()).hexdigest()[:16]
            out.append(Example(id=f"real-{cid}", text=ch, spans=[], carrier=carrier, source=src,
                               meta={"license": lic}))
    counts["chunks_kept"] = len(out)
    write_jsonl(a.out, out)
    Path(a.out).with_suffix(".counts.json").write_text(json.dumps(counts, indent=1))
    print(json.dumps(counts, indent=1))


def is_holdout(ex_id: str) -> bool:
    """A fixed ~2.5% of pool chunks, never mined or trained on; used only to track the real-code flag rate."""
    return hashlib.sha256(f"holdout:{ex_id}".encode()).digest()[0] < 6


def cmd_flagrate(a) -> None:
    from castellan.bundle import load_bundle

    hold = [e for e in read_jsonl(a.pool) if is_holdout(e.id)][: a.n]
    b = load_bundle(a.bundle, device=a.device)
    if a.two_stage:
        from castellan.stage2 import detector_factory

        scored = detector_factory(b, a.bundle).predict(hold)
    else:
        scored = b.score_many([e.text for e in hold])
    n_flag = sum(1 for st in scored if st.findings)
    n_spans = sum(len(st.findings) for st in scored)
    n_chars = sum(sum(f.end - f.start for f in st.findings) for st in scored)
    tot = sum(len(e.text) for e in hold)
    res = {"bundle": a.bundle, "two_stage": bool(a.two_stage), "n_chunks": len(hold), "flag_rate": n_flag / max(len(hold), 1),
           "spans_per_chunk": n_spans / max(len(hold), 1), "char_rate": n_chars / max(tot, 1)}
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


def cmd_mine(a) -> None:
    from castellan.bundle import load_bundle
    from castellan.spans import Finding

    pool = list(read_jsonl(a.pool))
    used = set()
    for p in a.skip or []:
        if Path(p).exists():
            used |= {e.id for e in read_jsonl(p)}  # not str.splitlines: code can hold U+2028
        sp = Path(p).with_suffix(".scored_ids.json")
        if sp.exists():
            used |= set(json.loads(sp.read_text()))
    r = random.Random(a.seed)
    fresh = [e for e in pool if e.id not in used and not is_holdout(e.id)]
    r.shuffle(fresh)
    batch = fresh[: a.n_score]
    fpath = Path(a.out).with_suffix(".findings.json")
    if fpath.exists() and json.loads(fpath.read_text())["bundle"] == a.bundle:
        cached = json.loads(fpath.read_text())
        by_id = {e.id: e for e in pool}
        batch = [by_id[i] for i in cached["ids"]]
        found = [[Finding(*f) for f in fs] for fs in cached["findings"]]
    else:
        b = load_bundle(a.bundle, device=a.device)
        found = [st.findings for st in b.score_many([e.text for e in batch])]
        fpath.write_text(json.dumps({"bundle": a.bundle, "ids": [e.id for e in batch],
                                     "findings": [[[f.start, f.end, f.label, f.score] for f in fs] for fs in found]}))
    scored = [type("S", (), {"findings": fs}) for fs in found]
    counts = collections.Counter()
    buckets = collections.defaultdict(collections.Counter)
    counts["chunks_scored"] = len(batch)
    keep = []
    for ex, st in zip(batch, scored):
        if not st.findings:
            counts["chunks_no_candidate"] += 1
            continue
        counts["chunks_with_candidates"] += 1
        bad = False
        for f in st.findings:
            counts["candidates_total"] += 1
            rs = candidate_reasons(ex.text, f.start, f.end)
            bk = bucket(ex.text[f.start:f.end])
            ctx = bool(HASH_CONTEXT.search(line_context(ex.text, f.start, f.end)))
            buckets[bk]["total"] += 1
            for reason in rs:
                counts[f"candidate_reason:{reason}"] += 1
            if rs:
                counts["candidates_dropped"] += 1
                bad = True
            else:
                counts["candidates_kept"] += 1
                buckets[bk]["kept"] += 1
                buckets[bk]["kept_with_hash_context"] += int(ctx)
        if bad:
            counts["chunks_dropped_by_candidate"] += 1
            continue
        keep.append((max(f.score for f in st.findings), len(st.findings), ex))
    keep.sort(key=lambda t: (-t[0], -t[1]))
    chosen = [ex for _, _, ex in keep[: a.top]]
    counts["chunks_eligible"] = len(keep)
    counts["chunks_chosen"] = len(chosen)
    if counts["candidates_total"]:
        counts["frac_candidates_kept"] = round(counts["candidates_kept"] / counts["candidates_total"], 4)
    counts["frac_chunks_flagged"] = round(counts["chunks_with_candidates"] / max(len(batch), 1), 4)
    counts["buckets"] = {k: {**v, "keep_rate": round(v["kept"] / max(v["total"], 1), 4)} for k, v in sorted(buckets.items())}
    write_jsonl(a.out, chosen)
    Path(a.out).with_suffix(".scored_ids.json").write_text(json.dumps([e.id for e in batch]))
    Path(a.out).with_suffix(".counts.json").write_text(json.dumps(counts, indent=1))
    print(json.dumps(counts, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--out", required=True)
    p.add_argument("--shards", type=int, default=2)
    p.add_argument("--n-files", type=int, default=30000)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_prepare)
    f = sub.add_parser("flagrate")
    f.add_argument("--bundle", required=True)
    f.add_argument("--pool", required=True)
    f.add_argument("--n", type=int, default=1000)
    f.add_argument("--device", default="mps")
    f.add_argument("--out", required=True)
    f.add_argument("--two-stage", action="store_true", help="use the bundle's stage-2 detector")
    f.set_defaults(fn=cmd_flagrate)
    m = sub.add_parser("mine")
    m.add_argument("--bundle", required=True)
    m.add_argument("--pool", required=True)
    m.add_argument("--skip", nargs="*", default=[])
    m.add_argument("--n-score", type=int, default=8000)
    m.add_argument("--top", type=int, default=1500)
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--device", default="mps")
    m.add_argument("--out", required=True)
    m.set_defaults(fn=cmd_mine)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
