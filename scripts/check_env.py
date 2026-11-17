"""Compare the running Python and installed packages with a lockfile. Warns, never installs.

    python scripts/check_env.py py312        # checks requirements-lock-py312.txt
    python scripts/check_env.py py314 --strict

The published results came from two environments (see README, "Which
environment produced what"). Exit code is 0 with warnings, or 1 with
--strict when anything differs.
"""
from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITLEAKS_VERSION = "8.30.1"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(path: Path) -> tuple[str | None, dict[str, str]]:
    py, pins = None, {}
    for line in path.read_text().splitlines():
        m = re.match(r"# Python (\S+),", line)
        if m:
            py = m.group(1)
        line = line.split("#", 1)[0].strip() if not line.startswith("#") else ""
        if "==" in line:
            n, v = line.split("==", 1)
            pins[norm(n)] = v.strip()
        elif " @ " in line:
            n, url = line.split(" @ ", 1)
            m = re.search(r"-(\d+(?:\.\d+)+)-py", url)
            pins[norm(n)] = m.group(1) if m else "?"
    return py, pins


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("env", choices=["py312", "py314"])
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--gitleaks", action="store_true", help="also check the gitleaks CLI version")
    a = ap.parse_args(argv)

    lock = ROOT / f"requirements-lock-{a.env}.txt"
    want_py, pins = locked(lock)
    have = {norm(d.metadata["Name"]): d.version for d in metadata.distributions()}
    issues = []
    if want_py and platform.python_version() != want_py:
        issues.append(f"python {platform.python_version()} (lock: {want_py})")
    for n, v in sorted(pins.items()):
        if n in ("pip", "setuptools", "wheel"):
            continue
        if n not in have:
            issues.append(f"{n} missing (lock: {v})")
        elif have[n] != v:
            issues.append(f"{n} {have[n]} (lock: {v})")
    if a.gitleaks:
        exe = shutil.which("gitleaks")
        gv = subprocess.run([exe, "version"], capture_output=True, text=True).stdout.strip() if exe else "missing"
        if gv.lstrip("v") != GITLEAKS_VERSION:
            issues.append(f"gitleaks {gv} (results used {GITLEAKS_VERSION})")
    if issues:
        print(f"WARNING: environment differs from {lock.name}; numbers may not match exactly:")
        for i in issues:
            print(f"  - {i}")
        return 1 if a.strict else 0
    print(f"environment matches {lock.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
