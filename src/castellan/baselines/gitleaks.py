"""Gitleaks CLI wrapper that returns character spans.

We run ``gitleaks dir`` once over a temp directory holding one file per
example, with the default rules and default settings (including its default
recursive decoding, max-decode-depth 5). The report is read in memory and
deleted. Values are used only to locate offsets and are never logged.

Gitleaks columns are byte based, 1-based, and drift by one for each line
after the first, so we do not trust them directly. We locate the reported
secret string in the text, preferring the occurrence on the reported start
line. If the secret is not in the text (gitleaks decoded it), we fall back to
converting the reported columns to char offsets on the reported line.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Sequence

from castellan.data.schema import Example
from castellan.evaluate import DocPrediction
from castellan.spans import Finding

# Rule id substrings -> our labels. First match wins; the default is SECRET_GENERIC.
RULE_MAP: tuple[tuple[str, str], ...] = (
    ("private-key", "SECRET_PRIVATE_KEY"),
    ("jwt", "SECRET_JWT"),
    ("github", "SECRET_VCS_TOKEN"),
    ("gitlab", "SECRET_VCS_TOKEN"),
    ("bitbucket", "SECRET_VCS_TOKEN"),
    ("openai", "SECRET_AI_KEY"),
    ("anthropic", "SECRET_AI_KEY"),
    ("huggingface", "SECRET_AI_KEY"),
    ("cohere", "SECRET_AI_KEY"),
    ("perplexity", "SECRET_AI_KEY"),
    ("aws", "SECRET_CLOUD_KEY"),
    ("gcp", "SECRET_CLOUD_KEY"),
    ("google", "SECRET_CLOUD_KEY"),
    ("azure", "SECRET_CLOUD_KEY"),
    ("alibaba", "SECRET_CLOUD_KEY"),
    ("digitalocean", "SECRET_CLOUD_KEY"),
    ("heroku", "SECRET_CLOUD_KEY"),
    ("postgres", "SECRET_DB_URL"),
    ("mongodb", "SECRET_DB_URL"),
    ("mysql", "SECRET_DB_URL"),
    ("redis", "SECRET_DB_URL"),
)


def rule_to_label(rule_id: str) -> str:
    r = rule_id.lower()
    for key, lab in RULE_MAP:
        if key in r:
            return lab
    return "SECRET_GENERIC"


def gitleaks_version(binary: str = "gitleaks") -> str:
    out = subprocess.run([binary, "version"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _byte_col_to_char(line: str, byte_col: int) -> int:
    """0-based byte column within a line -> 0-based char index (clamped)."""
    b = 0
    for i, ch in enumerate(line):
        if b >= byte_col:
            return i
        b += len(ch.encode("utf-8"))
    return len(line)


def locate(text: str, finding: dict) -> tuple[int, int, bool] | None:
    """Char span for one gitleaks finding. Returns (start, end, decoded)."""
    secret = finding.get("Secret") or ""
    starts = _line_starts(text)
    line0 = max(int(finding.get("StartLine", 1)) - 1, 0)
    if secret:
        occ = []
        i = text.find(secret)
        while i != -1:
            occ.append(i)
            i = text.find(secret, i + 1)
        if occ:
            line_of = lambda pos: max(j for j, s in enumerate(starts) if s <= pos)  # noqa: E731
            best = min(occ, key=lambda pos: abs(line_of(pos) - line0))
            return best, best + len(secret), False
    # fallback: reported columns (decoded secrets are not present verbatim)
    if line0 >= len(starts):
        return None
    end_line0 = min(max(int(finding.get("EndLine", line0 + 1)) - 1, line0), len(starts) - 1)
    quirk = 1 if line0 > 0 else 0
    ls, le = starts[line0], starts[end_line0]
    line_s = text[ls : (starts[line0 + 1] - 1) if line0 + 1 < len(starts) else len(text)]
    line_e = text[le : (starts[end_line0 + 1] - 1) if end_line0 + 1 < len(starts) else len(text)]
    a = ls + _byte_col_to_char(line_s, max(int(finding.get("StartColumn", 1)) - 1 - quirk, 0))
    b = le + _byte_col_to_char(line_e, max(int(finding.get("EndColumn", 1)) - (1 if end_line0 > 0 else 0), 0))
    if b <= a:
        return None
    return a, b, True


class GitleaksDetector:
    name = "gitleaks"

    def __init__(self, binary: str = "gitleaks", extra_args: Sequence[str] = ()) -> None:
        if shutil.which(binary) is None:
            raise FileNotFoundError("gitleaks binary not found on PATH")
        self.binary = binary
        self.extra_args = list(extra_args)
        self.version = gitleaks_version(binary)

    def predict(self, examples: Sequence[Example]) -> list[DocPrediction]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")}
        with tempfile.TemporaryDirectory(prefix="castellan-gl-") as tmp:
            src = os.path.join(tmp, "src")
            os.mkdir(src)
            for i, ex in enumerate(examples):
                with open(os.path.join(src, f"{i:07d}.txt"), "w", encoding="utf-8", newline="") as f:
                    f.write(ex.text)
            report = os.path.join(tmp, "report.json")
            cmd = [
                self.binary, "dir", src, "--report-format", "json", "--report-path", report,
                "--no-banner", "--exit-code", "0", "--log-level", "error", *self.extra_args,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if proc.returncode != 0:
                raise RuntimeError(f"gitleaks failed with exit code {proc.returncode}")
            with open(report, encoding="utf-8") as f:
                raw = json.load(f) or []
        by_doc: dict[int, list[Finding]] = {}
        for item in raw:
            i = int(os.path.splitext(os.path.basename(item["File"]))[0])
            loc = locate(examples[i].text, item)
            if loc is None:
                continue
            a, b, _decoded = loc
            by_doc.setdefault(i, []).append(Finding(a, b, rule_to_label(item.get("RuleID", "")), 1.0))
        raw.clear()
        return [DocPrediction(ex.id, sorted(by_doc.get(i, []), key=lambda f: (f.start, f.end))) for i, ex in enumerate(examples)]
