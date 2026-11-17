"""Lookalike generator: all-negative, deterministic, per-category checks.

Failure messages name the document id and category only, never the text.
"""

from __future__ import annotations

import base64
import json
import random
import re

import pytest

from castellan.cli import main
from castellan.data import lookalikes as L
from castellan.data.schema import read_jsonl, write_jsonl
from castellan.data.synth import generate

# Full-length vendor key shapes (public prefixes and lengths).
VENDOR_SHAPES = {
    "aws_key_id": re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b"),
    "aws_secret_example": re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{30}EXAMPLEKEY(?![A-Za-z0-9/+])"),
    "gcp_api_key": re.compile(r"\bAIza[\w-]{35}(?![\w-])"),
    "github_classic": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    "github_fine": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}"),
    "gitlab": re.compile(r"\bglpat-[\w-]{20}"),
    "openai": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "anthropic": re.compile(r"\bsk-ant-[\w-]{20,}"),
    "huggingface": re.compile(r"\bhf_[A-Za-z]{30,}"),
    "slack": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    "stripe": re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    "jwt": re.compile(r"\bey[A-Za-z0-9_-]{17,}\.ey[A-Za-z0-9_/-]{17,}\.[A-Za-z0-9_/-]{10,}"),
    "private_key": re.compile(r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY"),
}
# URL userinfo with a password part. Not vendor-prefixed, so it is checked separately:
# only the benign-by-form categories may put a value there.
URL_USERINFO = re.compile(r"\b[a-z][a-z0-9+]*://[^:/\s@]+:[^@\s]+@")


@pytest.fixture(scope="module")
def docs():
    return L.generate_lookalikes(3000, seed=7)


def _fail(ex, why):
    return f"{ex.id} ({ex.meta['category']}): {why}"


def test_all_negative_and_valid(docs):
    for ex in docs:
        assert ex.spans == [], _fail(ex, "has spans")
        assert ex.source == "synth"
        assert ex.id.startswith("lookalike-")
        assert ex.meta["families"] == ["neg:lookalike_" + ex.meta["category"]]
        assert ex.carrier in L.ALL_CARRIERS
        assert ex.text.strip(), _fail(ex, "empty text")
        assert "\u2014" not in ex.text, _fail(ex, "em-dash")


def test_every_category_and_carrier_covered(docs):
    seen = {(ex.meta["category"], ex.carrier) for ex in docs}
    for cat in L.CATEGORIES:
        got = {c for k, c in seen if k == cat.name}
        assert got == set(cat.carriers), f"{cat.name}: missing carriers {sorted(set(cat.carriers) - got)}"


def test_no_vendor_shapes_outside_doc_examples(docs):
    for ex in docs:
        for name, rx in VENDOR_SHAPES.items():
            for m in rx.finditer(ex.text):
                ok = ex.meta["category"] == "doc_example" and any(v in m.group(0) or m.group(0) in v for v in L.DOC_EXAMPLE_VALUES)
                assert ok, _fail(ex, f"matches vendor shape {name}")
        if URL_USERINFO.search(ex.text):
            assert L.CATEGORY_BY_NAME[ex.meta["category"]].cred_ok, _fail(ex, "URL password outside benign-by-form category")


def test_doc_examples_are_only_documented_values():
    r = random.Random(1)
    for _ in range(300):
        it = L.cat_doc_example(r)
        assert it.value in L.DOC_EXAMPLE_VALUES, "doc_example produced an undocumented value"
    for kid, secret in L.AWS_EXAMPLE_PAIRS:
        assert kid.endswith("EXAMPLE") and secret.endswith("EXAMPLEKEY")
    head = json.loads(base64.urlsafe_b64decode(L.RFC7519_JWT.split(".")[0] + "=="))
    assert head == {"typ": "JWT", "alg": "HS256"}


def test_deterministic_by_seed():
    a = [e.to_dict() for e in L.generate_lookalikes(200, seed=3)]
    b = [e.to_dict() for e in L.generate_lookalikes(200, seed=3)]
    c = [e.to_dict() for e in L.generate_lookalikes(200, seed=4)]
    assert a == b
    assert a != c


# per-category checks: each draws items directly from the category function


def _items(fn, n=400, seed=0):
    r = random.Random(seed)
    return [fn(r) for _ in range(n)]


_CRED_RX = re.compile(r"(?i)pass|secret|token|api_?key|auth|bearer|credential(?!s?_id)")
# names that gitleaks generic-api-key allowlists, or that are plainly not secrets
_ALLOWED_NAMES = {"UserSecretsId", "authorized_key"}


def _credish(key: str) -> bool:
    return key not in _ALLOWED_NAMES and bool(_CRED_RX.search(key))


def test_placeholder():
    for it in _items(L.cat_placeholder):
        v = it.value
        assert (
            v.startswith(("$", "{", "<", "%", "@"))
            or re.fullmatch(r"[A-Za-z_.-]+", v)
            or v in ("true", "false", "null", "None", "nil", '""')
            or it.form == "userinfo"
        ), f"placeholder form {it.form} has an unexpected shape"


def test_indirect_reference():
    for it in _items(L.cat_indirect_reference):
        v = it.value
        ok = bool(re.search(r"[\w.-]+[\[(].*[\])]", v)) or v.startswith(
            ("arn:aws:secretsmanager:", "secret/data/", "process.env.", "$(vault kv get ", "RUN --mount=type=secret,"))
        assert ok, it.form
        if it.form == "aws_sm_arn":
            assert ":123456789012:secret:" in v


def test_masked():
    for it in _items(L.cat_masked):
        v = it.value
        assert any(ch in v for ch in "*.•…") or "REDACTED" in v.upper() or set(v) <= set("xX"), it.form


def test_public_key():
    for it in _items(L.cat_public_key):
        assert "PRIVATE" not in it.value
        assert it.value.startswith(("ssh-ed25519 ", "ssh-rsa ", "-----BEGIN PUBLIC KEY-----", "-----BEGIN CERTIFICATE-----"))
        assert not _credish(it.key), it.key
        if it.form == "ssh_pub":
            blob = base64.b64decode(it.value.split()[1])
            n = int.from_bytes(blob[:4], "big")
            assert blob[4 : 4 + n] == it.value.split()[0].encode()


def test_hash_digest():
    for it in _items(L.cat_hash_digest):
        assert not _credish(it.key), it.key
        assert re.search(r"[0-9a-f]{7,}|h1:[A-Za-z0-9+/]{43}=", it.value), it.form


def test_uuid_id():
    uuid_rx = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", re.I)
    for it in _items(L.cat_uuid_id):
        assert not _credish(it.key), it.key
        assert re.search(r"(?i)id\b|Id$|_id|^id$", it.key), it.key
        if it.form.startswith("uuid"):
            assert uuid_rx.search(it.value)


def _is_sequential(s: str) -> bool:
    # mirrors detect-secrets is_sequential_string
    import string as S

    seqs = (S.ascii_uppercase * 2 + S.digits + "+/", S.digits + S.ascii_uppercase * 2 + "+/",
            (S.digits + S.ascii_uppercase) * 2, S.digits * 2, S.hexdigits.upper() * 2, S.ascii_uppercase + "=/")
    return any(s.upper() in q for q in seqs)


def test_sequential():
    for it in _items(L.cat_sequential):
        assert not _credish(it.key), it.key
        v = it.value
        assert _is_sequential(v) or len(set(v)) == 1 or v in (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",), it.form


def test_lockfile():
    for it in _items(L.cat_lockfile, n=200):
        assert it.block and it.fname
        assert it.fname in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Pipfile.lock", "poetry.lock", "Cargo.lock", "go.sum")
        assert re.search(r"sha512-|sha256:|checksum = \"[0-9a-f]{64}\"|h1:", it.value), it.fname


def test_benign_base64():
    for it in _items(L.cat_benign_base64):
        assert not re.search(r"(?i)pass|secret|bearer|auth", it.key), it.key
        if it.form == "data_uri":
            head, data = it.value.split(",", 1)
            assert head.startswith("data:") and head.endswith(";base64")
            base64.b64decode(data, validate=True)
        else:
            raw = base64.b64decode(it.value + "=" * (-len(it.value) % 4))
            raw.decode("utf-8")  # cursors and encoded text decode to plain text


def test_high_entropy_categories_use_neutral_framing(docs):
    for ex in docs:
        if L.CATEGORY_BY_NAME[ex.meta["category"]].cred_ok:
            continue
        assert "Authorization: Bearer" not in ex.text, _fail(ex, "bearer framing")
        assert "Password=" not in ex.text, _fail(ex, "password framing")
        assert ex.carrier != "connection_string"


# CLI


def test_cli_default_output_unchanged(tmp_path):
    out = tmp_path / "a.jsonl"
    ref = tmp_path / "ref.jsonl"
    assert main(["synth", "--n", "60", "--seed", "0", "--out", str(out)]) == 0
    write_jsonl(ref, generate(60, seed=0))
    assert out.read_bytes() == ref.read_bytes()


def test_cli_lookalikes(tmp_path):
    out = tmp_path / "b.jsonl"
    assert main(["synth", "--n", "30", "--seed", "0", "--lookalikes", "40", "--out", str(out)]) == 0
    exs = list(read_jsonl(out))
    assert len(exs) == 70
    assert [e.to_dict() for e in exs[:30]] == [e.to_dict() for e in generate(30, seed=0)]
    assert all(e.id.startswith("lookalike-") and not e.spans for e in exs[30:])
    assert len({e.id for e in exs}) == 70


def test_rfc_jwt_always_has_preceding_context(docs):
    # The RFC 7519 example has no in-string marker, so a causal probe needs context before it.
    seen = 0
    for ex in docs:
        pos = ex.text.find(L.RFC7519_JWT)
        if pos < 0:
            continue
        seen += 1
        before = ex.text[:pos]
        assert "RFC 7519" in before, _fail(ex, f"RFC JWT without preceding context ({ex.carrier})")
    assert seen > 50
