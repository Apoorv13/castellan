from __future__ import annotations

import json
import re

from castellan.guard import ALLOW, BLOCK, REDACT, Policy, guard, serialize
from castellan.spans import Finding


def regex_scanner(pattern: str, label: str):
    def scan(text):
        return [Finding(m.start(), m.end(), label, 0.9) for m in re.finditer(pattern, text)]

    return scan


def test_serialize_is_valid_json_and_leaves_line_up():
    obj = {"name": "run", "input": {"cmd": 'echo "hi"\n', "args": ["a", 3, None], "weird key": "v"}}
    text, leaves = serialize(obj)
    assert json.loads(text) == obj
    paths = {l.path for l in leaves}
    assert {"$.name", "$.input.cmd", "$.input.args[0]", '$.input["weird key"]'} <= paths


def test_block_on_secret_with_path():
    call = {"name": "http", "input": {"headers": {"Authorization": "Bearer ZZTOKENZZ"}}}
    d = guard(call, regex_scanner("ZZTOKENZZ", "SECRET_VCS_TOKEN"))
    assert d.action == BLOCK
    (f,) = d.findings
    assert f.path == "$.input.headers.Authorization"
    assert call["input"]["headers"]["Authorization"][f.start : f.end] == "ZZTOKENZZ"


def test_redact_pii_inside_escaped_string():
    call = {"input": {"body": 'line1\n"quoted" mail a@b.c end'}}
    d = guard(call, regex_scanner(r"a@b\.c", "EMAIL"))
    assert d.action == REDACT
    (f,) = d.findings
    assert call["input"]["body"][f.start : f.end] == "a@b.c"
    assert d.redacted["input"]["body"] == 'line1\n"quoted" mail [REDACTED:EMAIL] end'
    assert call["input"]["body"].endswith("a@b.c end")  # original untouched


def test_allow_when_nothing_found_and_below_min_score():
    call = {"x": "hello"}
    assert guard(call, lambda t: []).action == ALLOW
    d = guard(call, regex_scanner("hello", "EMAIL"), Policy(min_score=0.95))
    assert d.action == ALLOW and d.findings == []


def test_findings_on_keys_are_ignored():
    call = {"ZZTOKENZZ": "fine"}
    assert guard(call, regex_scanner("ZZTOKENZZ", "SECRET_GENERIC")).action == ALLOW


def test_decision_dict_has_no_values():
    call = {"k": "ZZTOKENZZ"}
    d = guard(call, regex_scanner("ZZTOKENZZ", "SECRET_GENERIC"))
    assert "ZZTOKENZZ" not in json.dumps(d.to_dict())
