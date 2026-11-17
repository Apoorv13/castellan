"""Agent tool-call guard: allow, redact or block.

The tool call (any JSON value) is serialized once with key names kept, so
the probe sees context like ``"api_key": "..."``. Each string leaf's position
in the serialized text is recorded, findings are mapped back to the leaf's
JSON path and to offsets inside the leaf's raw (unescaped) string, and the
policy decides what to do.

Decisions and findings carry class, score, JSON path and offsets only.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from castellan.data.schema import SECRET_LABELS
from castellan.spans import Finding

Scanner = Callable[[str], Sequence[Finding]]

ALLOW, REDACT, BLOCK = "allow", "redact", "block"
_ORDER = {ALLOW: 0, REDACT: 1, BLOCK: 2}


@dataclass
class Policy:
    """Action per label, applied when a finding's score is at least ``min_score``."""

    actions: dict[str, str] = field(default_factory=lambda: {lab: BLOCK for lab in SECRET_LABELS})
    default_action: str = REDACT
    min_score: float = 0.0
    mask: str = "[REDACTED:{label}]"

    def action_for(self, f: Finding) -> str:
        if f.score < self.min_score:
            return ALLOW
        return self.actions.get(f.label, self.default_action)


@dataclass
class Leaf:
    path: str
    start: int  # offsets of the escaped string content in the serialized text
    end: int
    raw: str


@dataclass
class GuardDecision:
    action: str
    findings: list[Finding]  # offsets are within the leaf's raw string; path is set
    redacted: Any = None

    def to_dict(self) -> dict:
        return {"action": self.action, "findings": [f.to_dict() for f in self.findings]}


def _path_join(base: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{base}[{key}]"
    k = str(key)
    if k.isidentifier():
        return f"{base}.{k}"
    return f"{base}[{json.dumps(k)}]"


def serialize(obj: Any) -> tuple[str, list[Leaf]]:
    """JSON text plus the location of every string leaf (values and keys are context only)."""
    parts: list[str] = []
    leaves: list[Leaf] = []
    n = 0

    def emit(s: str) -> None:
        nonlocal n
        parts.append(s)
        n += len(s)

    def walk(o: Any, path: str) -> None:
        if isinstance(o, dict):
            emit("{")
            for i, (k, v) in enumerate(o.items()):
                if i:
                    emit(", ")
                emit(json.dumps(str(k), ensure_ascii=False) + ": ")
                walk(v, _path_join(path, k))
            emit("}")
        elif isinstance(o, (list, tuple)):
            emit("[")
            for i, v in enumerate(o):
                if i:
                    emit(", ")
                walk(v, _path_join(path, i))
            emit("]")
        elif isinstance(o, str):
            esc = json.dumps(o, ensure_ascii=False)
            leaves.append(Leaf(path, n + 1, n + len(esc) - 1, o))
            emit(esc)
        else:
            emit(json.dumps(o, ensure_ascii=False))

    walk(obj, "$")
    return "".join(parts), leaves


def _escaped_to_raw(raw: str) -> list[int]:
    """For each escaped-content position (plus the end), the raw char index."""
    m: list[int] = []
    for i, ch in enumerate(raw):
        m.extend([i] * len(json.dumps(ch, ensure_ascii=False)[1:-1]))
    m.append(len(raw))
    return m


def locate_findings(findings: Sequence[Finding], leaves: Sequence[Leaf]) -> list[Finding]:
    """Map serialized-text findings to (path, raw offsets). Findings outside leaves are dropped."""
    out = []
    for f in findings:
        for leaf in leaves:
            a, b = max(f.start, leaf.start), min(f.end, leaf.end)
            if a >= b:
                continue
            m = _escaped_to_raw(leaf.raw)
            ra, rb = m[a - leaf.start], m[b - leaf.start]
            if rb > ra:
                out.append(Finding(ra, rb, f.label, f.score, path=leaf.path))
    return out


def _set_path(obj: Any, path: str, value: str) -> None:
    """Set a leaf by path produced by ``_path_join``."""
    tokens: list[Any] = []
    i = 1
    while i < len(path):
        if path[i] == ".":
            j = i + 1
            while j < len(path) and path[j] not in ".[":
                j += 1
            tokens.append(path[i + 1 : j])
            i = j
        else:  # "["
            j = path.index("]", i) if path[i + 1] != '"' else path.index('"]', i + 2) + 1
            inner = path[i + 1 : j]
            tokens.append(json.loads(inner) if inner.startswith('"') else int(inner))
            i = j + 1
    cur = obj
    for t in tokens[:-1]:
        cur = cur[t]
    cur[tokens[-1]] = value


def redact(obj: Any, findings: Sequence[Finding], mask: str) -> Any:
    out = copy.deepcopy(obj)
    leaves = {leaf.path: leaf.raw for leaf in serialize(obj)[1]}
    by_path: dict[str, list[Finding]] = {}
    for f in findings:
        by_path.setdefault(f.path or "$", []).append(f)
    for path, fs in by_path.items():
        raw = leaves[path]
        for f in sorted(fs, key=lambda f: -f.start):
            raw = raw[: f.start] + mask.format(label=f.label) + raw[f.end :]
        if path == "$":
            out = raw
        else:
            _set_path(out, path, raw)
    return out


def guard(tool_call: Any, scanner: Scanner, policy: Policy | None = None) -> GuardDecision:
    policy = policy or Policy()
    text, leaves = serialize(tool_call)
    findings = locate_findings(scanner(text), leaves)
    acted = [f for f in findings if policy.action_for(f) != ALLOW]
    action = max((policy.action_for(f) for f in acted), key=_ORDER.__getitem__, default=ALLOW)
    redacted = None
    if action == REDACT:
        redacted = redact(tool_call, acted, policy.mask)
    return GuardDecision(action, acted, redacted)
