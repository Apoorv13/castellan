"""Rebuild the frozen eval sets and verify them against the freeze manifests.

The eval JSONL files are not stored in git. Two ways to get them:

    python scripts/eval_data.py                          # regenerate from the frozen generators
    python scripts/eval_data.py --from-asset castellan-eval-data-v1.tar.gz   # unpack the release asset

Either way, every file listed in eval_data/FROZEN.sha256 and
eval_data/FROZEN_ADDENDUM.sha256 (the JSONL outputs, the generators and
src/castellan/evaluate.py) is then checked against its SHA-256. The script exits
non-zero on any mismatch. The generators are deterministic: regenerating gives
byte-identical files (checked on Python 3.12 and 3.14).

    python scripts/eval_data.py --verify-only            # check existing files, write nothing

Prints file names and OK or MISMATCH only, never contents.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval_data"
MANIFESTS = (EVAL / "FROZEN.sha256", EVAL / "FROZEN_ADDENDUM.sha256")
GENERATORS = ("secrets_eval.py", "ladder.py", "ladder_neutral.py")


def manifest() -> dict[str, str]:
    out: dict[str, str] = {}
    for m in MANIFESTS:
        for line in m.read_text().splitlines():
            if line.strip():
                digest, path = line.split(maxsplit=1)
                out[path.strip()] = digest
    return out


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify() -> bool:
    ok = True
    for rel, want in manifest().items():
        p = ROOT / rel
        if not p.exists():
            print(f"MISSING   {rel}")
            ok = False
        elif sha256(p) != want:
            print(f"MISMATCH  {rel}")
            ok = False
        else:
            print(f"OK        {rel}")
    return ok


def regenerate() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
    for g in GENERATORS:
        print(f"generating with eval_data/{g}")
        subprocess.run([sys.executable, str(EVAL / g), "--out", str(EVAL)], check=True, env=env,
                       stdout=subprocess.DEVNULL)


def unpack(asset: Path) -> None:
    allowed = {Path(rel).name for rel in manifest() if rel.endswith(".jsonl")}
    with tarfile.open(asset, "r:gz") as tf:
        for m in tf.getmembers():
            name = Path(m.name).name
            # Only regular files with an expected name; nothing else is written.
            if not m.isfile() or name not in allowed:
                continue
            src = tf.extractfile(m)
            assert src is not None
            (EVAL / name).write_bytes(src.read())
            print(f"unpacked  eval_data/{name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--from-asset", type=Path, help="castellan-eval-data-*.tar.gz release asset")
    g.add_argument("--verify-only", action="store_true")
    a = ap.parse_args(argv)
    if a.from_asset:
        unpack(a.from_asset)
    elif not a.verify_only:
        regenerate()
    ok = verify()
    print("all frozen hashes match" if ok else "FROZEN HASH CHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
