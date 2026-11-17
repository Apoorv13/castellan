"""Build the gzipped release assets (deterministic: fixed mtimes, owners and order).

    python scripts/make_release_assets.py --runs runs --out dist

Writes:
  dist/castellan-eval-data-v1.tar.gz  the frozen eval JSONL files plus both FROZEN manifests
                                      (data license: CC-BY-4.0, see LICENSE-DATA.md)
  dist/castellan-m3-runs-v1.tar.gz    run outputs that scripts/reproduce.sh tables needs:
                                      baseline and probe predictions (ids, offsets, labels,
                                      scores), readability flags, and the reference reports
  dist/SHA256SUMS

The eval files are verified against the manifests first. Run outputs are
checked to hold no long strings (no text, values or model output), and the
script refuses to build if one does. Prints names, sizes and hashes only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from eval_data import manifest, sha256, verify  # noqa: E402

SIZES = ("q06", "q17", "q4", "q8")
# Longest legitimate string in a run output is a 64-char SHA-256.
MAX_STR = 64


def _strings(o, acc: list[str]) -> list[str]:
    if isinstance(o, dict):
        for k, v in o.items():
            acc.append(k)
            _strings(v, acc)
    elif isinstance(o, list):
        for v in o:
            _strings(v, acc)
    elif isinstance(o, str):
        acc.append(o)
    return acc


def check_no_values(p: Path) -> None:
    if p.suffix == ".jsonl":
        objs = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    elif p.suffix == ".json":
        objs = [json.loads(p.read_text())]
    else:
        return
    longest = max((len(s) for s in _strings(objs, [])), default=0)
    if longest > MAX_STR:
        raise SystemExit(f"refusing: {p.name} has a string of length {longest} (> {MAX_STR})")


def build(out: Path, members: list[tuple[str, Path]]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for arc, src in sorted(members):
            data = src.read_bytes()
            ti = tarfile.TarInfo(arc)
            ti.size, ti.mtime, ti.mode = len(data), 0, 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))
    with out.open("wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue())
    print(f"{out.name}: {len(members)} files, {out.stat().st_size} bytes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=Path, default=ROOT / "runs", help="directory holding baselines/ and m3/")
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)

    if not verify():
        print("eval files do not match the freeze manifests; run scripts/eval_data.py first")
        return 1
    ev = [(rel, ROOT / rel) for rel in manifest() if rel.endswith(".jsonl")]
    ev += [(f"eval_data/{m}", ROOT / "eval_data" / m) for m in ("FROZEN.sha256", "FROZEN_ADDENDUM.sha256")]
    build(a.out / "castellan-eval-data-v1.tar.gz", ev)

    r = a.runs
    files: list[Path] = sorted((r / "baselines").rglob("*.json"))
    for n in SIZES:
        files += sorted((r / "m3" / n).rglob("*.json"))
        files += [r / "m3" / f"readability_summary_{n}.json"]
        files += sorted((r / "m3" / "readability").glob(f"{n}__*.jsonl"))
    refs = [r / "m3" / f for f in ("report.json", "analysis.json", "tables.md", "analysis_tables.md")]
    refs += [r / f for f in ("gate_probe-v1.json", "final_probe-v1.json")]
    members: list[tuple[str, Path]] = []
    for p in files:
        check_no_values(p)
        members.append((f"runs/{p.relative_to(r)}", p))
    # Reference copies: what reproduce.sh compares its rebuilt tables against.
    for p in refs:
        check_no_values(p)
        members.append((f"runs/reference/{p.name}", p))
    build(a.out / "castellan-m3-runs-v1.tar.gz", members)

    sums = [f"{sha256(p)}  {p.name}" for p in sorted(a.out.glob("castellan-*.tar.gz"))]
    (a.out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print("\n".join(sums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
