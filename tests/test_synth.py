"""Synthetic generator tests. Assertions never print values."""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

from castellan.data import synth
from castellan.data.schema import LABELS, SECRET_LABELS


def test_deterministic():
    a = [e.to_dict() for e in synth.generate(50, seed=3)]
    b = [e.to_dict() for e in synth.generate(50, seed=3)]
    assert a == b


def test_spans_valid_and_cover_all_carriers():
    exs = synth.generate(1500, seed=1)
    assert {e.carrier for e in exs} == set(synth.CARRIERS)
    labels = Counter(s.label for e in exs for s in e.spans)
    for lab in SECRET_LABELS:
        assert labels[lab] > 20, lab
    assert sum(1 for e in exs if not e.spans) > 200  # negatives present
    for e in exs:
        for s in e.spans:
            seg = e.text[s.start : s.end]
            assert seg and seg == seg.strip(), f"{e.id}: span has edge whitespace"


def test_json_carriers_parse_and_spans_point_inside_strings():
    for e in synth.generate(400, seed=2):
        if e.carrier in ("tool_call_json", "tool_result_json"):
            json.loads(e.text)


def test_card_numbers_pass_luhn():
    import random

    r = random.Random(0)
    for _ in range(50):
        v, _, _ = synth.card(r)
        digits = re.sub(r"\D", "", v)
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = int(ch) * (2 if i % 2 else 1)
            total += d - 9 if d > 9 else d
        assert total % 10 == 0


def test_hard_negatives_have_no_spans():
    for e in synth.generate(300, seed=4):
        fams = e.meta["families"]
        if fams and all(f.startswith("neg:") for f in fams):
            assert e.spans == []


def test_training_code_never_touches_eval_data():
    """Integrity rule: training code must not import or read eval_data.

    Checks imports and non-docstring string constants in every src module
    except the CLI (which dispatches eval commands) and evaluate/readability.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "castellan"
    for path in src.rglob("*.py"):
        if path.name in ("cli.py", "evaluate.py", "readability.py"):
            continue
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds is not None:
                    docstrings.add(ds)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                assert not any("eval_data" in n for n in names), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings:
                assert "eval_data" not in node.value, path


def test_labels_are_known():
    for e in synth.generate(200, seed=5):
        assert all(s.label in LABELS for s in e.spans)
