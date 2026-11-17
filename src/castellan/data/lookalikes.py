"""Benign lookalike documents: things that look like secrets but are not.

Every document is an all-negative training example (no spans). The
categories come from false-positive classes that secret scanners already
publish, so they reflect public knowledge, not our eval sets. Each category
function cites its source in its docstring.

Sources (fetched 2026-11-16):

- [GL] Gitleaks default config, tag v8.30.1 (commit 83d9cd684c87d95d656c1458ef04895a7f1cbd8e),
  https://github.com/gitleaks/gitleaks/blob/v8.30.1/config/gitleaks.toml
- [DS] detect-secrets heuristic filters, master at commit 5e141933554a0b74e7341841f318be21e895339c,
  https://github.com/Yelp/detect-secrets/blob/5e141933554a0b74e7341841f318be21e895339c/detect_secrets/filters/heuristic.py
- [AWS-IAM] https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html
- [AWS-CLI] https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html
- [AWS-SAR] https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssecretsmanager.html
- [VAULT] https://developer.hashicorp.com/vault/docs/commands/kv/get
- [RFC7519] https://www.rfc-editor.org/rfc/rfc7519.txt, section 3.1 "Example JWT"
- [RFC2397] https://www.rfc-editor.org/rfc/rfc2397.txt, section 3 "Syntax" and section 4 "Examples"
- [SLACK-PAG] https://api.slack.com/docs/pagination, section "Cursor-based pagination"

Design rules:

- One category per document, so per-category counts and flag rates are clean.
- Context is the label. In synth.py, random hex and random alphanumerics
  under names like ``password`` or ``api_secret`` are positives. So the
  high-entropy categories here (hashes, IDs, public keys, lockfiles, base64)
  only use benign key names and neutral framing (no ``Authorization:
  Bearer``, no ``Password=``). Credential-style names and framing are used
  only for categories that are benign by their form: placeholders,
  documented examples, masked values and indirect references.
- Nothing here matches a vendor-prefixed key shape except the documented
  example keys (category ``doc_example``). tests/test_lookalikes.py checks it.
- Deterministic by seed: all randomness comes from one ``random.Random``.

Excluded items (things we left out because we could not be sure they are
not credentials) are listed in src/castellan/data/README.md.

This module must never import or read eval_data/.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import struct
from dataclasses import dataclass
from typing import Callable

from castellan.data.schema import Example

ALNUM = string.ascii_letters + string.digits
UPPER_DIGITS = string.ascii_uppercase + string.digits
HEX = "0123456789abcdef"


def _rs(r: random.Random, alphabet: str, n: int) -> str:
    return "".join(r.choice(alphabet) for _ in range(n))


@dataclass
class Item:
    """One benign value. ``key`` is its field name; ``block`` marks multi-line content."""

    key: str
    value: str
    form: str
    block: bool = False
    fname: str = ""  # for blocks: the file the content came from
    lead: str = ""  # optional context sentence (prose and chat carriers)


# credential-style names, only for categories that are benign by form
CRED_NAMES = [
    "API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITLAB_TOKEN", "HF_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "DATABASE_PASSWORD", "client_secret", "password",
    "secret_key", "access_token", "auth_token", "SLACK_BOT_TOKEN", "STRIPE_SECRET_KEY",
    "webhook_secret", "jwt_secret", "SMTP_PASSWORD", "REDIS_PASSWORD",
]
ENV_VARS = [n.upper() for n in CRED_NAMES]

# ---------------------------------------------------------------------------
# categories


def cat_placeholder(r: random.Random) -> Item:
    """Placeholders and template variables.

    Sources:
    - [GL] global ``[allowlist] regexes``: ``^\\$(?:[A-Z_]+|[a-z_]+)$``,
      ``^\\${(?:[A-Z_]+|[a-z_]+)}$``, ``^\\{\\{[ \\t]*[\\w ().|]+[ \\t]*}}$``,
      ``^\\$\\{\\{[ \\t]*(?:(?:env|github|secrets|vars)(?:\\.[A-Za-z]\\w+)+...)}}$``,
      ``^%(?:[A-Z_]+|[a-z_]+)%$``, ``^%[+\\-# 0]?[bcdeEfFgGoOpqstTUvxX]$``, ``^\\{\\d{0,2}}$``,
      ``^@(?:[A-Z_]+|[a-z_]+)@$``, ``(?i)^true|false|null$``.
    - [GL] rule ``generic-api-key`` allowlist regex ``^[a-zA-Z_.-]+$`` (letters-only values
      such as ``your_api_key_here``).
    - [GL] rule ``curl-auth-user`` allowlist: ``[^:]+:(?:change(?:it|me)|pass(?:word)?|pwd|test|token|\\*+|x+)``,
      ``<[^>]+>:<[^>]+>``, ``[^:]+:\\$(?:\\d|\\w+|\\{(?:\\d|\\w+)})``.
    - [DS] ``is_templated_secret`` (``{secret}``, ``<secret>``, ``${secret}``) and
      ``is_prefixed_with_dollar_sign``.
    """
    key = r.choice(CRED_NAMES)
    env = r.choice(ENV_VARS)
    low = env.lower()
    forms = {
        "dollar": f"${env}",
        "dollar_brace": f"${{{env}}}",
        "mustache": "{{ " + r.choice([low, f"secrets.{low}", f".Values.{low}", f"env.{env}"]) + " }}",
        "gha_expr": "${{ " + r.choice(["secrets", "env", "vars"]) + f".{env}" + " }}",
        "percent": f"%{env}%",
        "printf": r.choice(["%s", "%v", "%q", "%d"]),
        "format_index": r.choice(["{}", "{0}", "{1}"]),
        "at_var": f"@{env}@",
        "angle": r.choice([f"<{low}>", "<your-api-key>", "<YOUR_TOKEN>", "<password>", "<secret>", "<insert key here>"]),
        "brace": r.choice([f"{{{low}}}", "{api_key}", "{token}", "{password}"]),
        "letters_only": r.choice([
            "your_api_key_here", "your-api-key", "YOUR_SECRET_KEY", "insert-token-here", "replace_me",
            "changeme", "change-it", "my-secret-password", "not-a-real-key", "TODO", "placeholder",
        ]),
        "literal": r.choice(["true", "false", "null", "None", "nil", '""']),
        "userinfo": r.choice([
            f"{r.choice(['admin', 'user', 'app', 'deploy'])}:{r.choice(['changeme', 'changeit', 'password', 'pass', 'pwd', 'test', 'token'])}",
            "<user>:<password>", "<username>:<token>",
            f"{r.choice(['admin', 'user', 'ci'])}:${r.choice(['PASSWORD', 'TOKEN', 'API_TOKEN', '1'])}",
            f"{r.choice(['admin', 'user', 'ci'])}:${{{r.choice(['PASSWORD', 'TOKEN', '1'])}}}",
        ]),
    }
    form = r.choice(list(forms))
    value = forms[form]
    if form == "userinfo":
        key = r.choice(["basic_auth", "credentials", "userinfo", "auth"])
    return Item(key, value, form)


# AWS documented example keys: [AWS-IAM] and [AWS-CLI].
AWS_EXAMPLE_PAIRS = [
    ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("AKIAI44QH8DHBEXAMPLE", "je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY"),
]
# The only self-evidently synthetic entry of the gitleaks gcp-api-key allowlist.
GCP_EXAMPLE_KEY = "AIzaSyabcdefghijklmnopqrstuvwxyz1234567"
# RFC 7519 section 3.1, with the display line breaks removed.
RFC7519_JWT = (
    "eyJ0eXAiOiJKV1QiLA0KICJhbGciOiJIUzI1NiJ9"
    ".eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
    ".dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)
DOC_EXAMPLE_VALUES = frozenset({v for pair in AWS_EXAMPLE_PAIRS for v in pair} | {GCP_EXAMPLE_KEY, RFC7519_JWT})


def cat_doc_example(r: random.Random) -> Item:
    """Documented example credentials published by vendors and standards.

    Sources:
    - [AWS-IAM] "Manage access keys for IAM users": ``AKIAIOSFODNN7EXAMPLE`` and
      ``wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY``.
    - [AWS-CLI] "Configuration and credential file settings": the pair above plus
      ``AKIAI44QH8DHBEXAMPLE`` and ``je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY``.
    - [GL] rule ``aws-access-token`` allowlist regex ``.+EXAMPLE$``.
    - [GL] rule ``gcp-api-key`` allowlist entry ``AIzaSyabcdefghijklmnopqrstuvwxyz1234567``.
    - [RFC7519] section 3.1 "Example JWT" (HMAC key published in RFC 7515 appendix A.1).
    """
    k = r.random()
    if k < 0.35:
        kid, secret = r.choice(AWS_EXAMPLE_PAIRS)
        if r.random() < 0.5:
            item = Item(r.choice(["aws_access_key_id", "AWS_ACCESS_KEY_ID", "AccessKeyId"]), kid, "aws_key_id")
        else:
            item = Item(r.choice(["aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "SecretAccessKey"]), secret, "aws_secret")
        item.lead = r.choice(["", "Using the example credentials from the AWS documentation.",
                              "This is the sample key pair from the AWS CLI docs."])
    elif k < 0.6:
        item = Item(r.choice(["GOOGLE_API_KEY", "api_key", "maps_api_key"]), GCP_EXAMPLE_KEY, "gcp_example")
        item.lead = r.choice(["", "Placeholder key for the docs build.", "Dummy key used in the README example."])
    else:
        item = Item(r.choice(["id_token", "token", "jwt", "example_jwt"]), RFC7519_JWT, "rfc7519_jwt")
        # Unlike the AWS and GCP examples, this value has no marker inside it, so it
        # always comes with a context line before it (a causal probe only sees the past).
        item.lead = r.choice(["This is the example JWT from RFC 7519 section 3.1.",
                              "Test vector copied from the JWT spec (RFC 7519 section 3.1).",
                              "RFC 7519 example token, not a real session."])
    return item


def cat_indirect_reference(r: random.Random) -> Item:
    """Indirect references: the value is looked up elsewhere, not written inline.

    Sources:
    - [DS] ``is_indirect_reference``: lines like ``secret = get_secret_key()`` or
      ``secret = request.headers['apikey']`` (regex ``[\\w.-]+[\\[\\(][^\\v]*[\\]\\)]`` after an
      assignment or comparison).
    - [AWS-SAR] secret ARN format ``arn:${Partition}:secretsmanager:${Region}:${Account}:secret:${SecretId}``;
      account ``123456789012`` is the example account used in [AWS-CLI].
    - [VAULT] ``vault kv get -mount=secret -field=passcode creds``.
    - [GL] rule ``generic-api-key`` line allowlist ``--mount=type=secret,`` (Docker build secrets).
    """
    env = r.choice(ENV_VARS)
    name = r.choice(["prod/db-password", "app/api-key", "ci/github-token", "payments/stripe", "svc/llm-key"])
    forms = {
        "py_environ": r.choice([f'os.environ["{env}"]', f"os.environ['{env}']", f'os.getenv("{env}")',
                                f'os.environ.get("{env}", "")']),
        "js_env": f"process.env.{env}",
        "ruby_env": r.choice([f'ENV["{env}"]', f'ENV.fetch("{env}")']),
        "go_env": f'os.Getenv("{env}")',
        "getter_call": r.choice(["get_secret_key()", f'get_secret("{name}")', f'secrets.get("{env.lower()}")',
                                 f'config.get("{env.lower()}")', f'load_credential("{name}")']),
        "header_lookup": r.choice(["request.headers['apikey']", 'request.headers["Authorization"]',
                                   "req.headers['x-api-key']"]),
        "aws_sm_arn": (f"arn:aws:secretsmanager:{r.choice(['us-east-1', 'us-west-2', 'eu-west-1'])}"
                       f":123456789012:secret:{name}-{_rs(r, string.ascii_letters, 6)}"),
        "vault_cli": f"$(vault kv get -mount=secret -field={r.choice(['passcode', 'password', 'token', 'api_key'])} {r.choice(['creds', 'app', 'ci'])})",
        "vault_path": f"secret/data/{r.choice(['app', 'ci', 'prod'])}/{r.choice(['db', 'api', 'llm'])}#{r.choice(['password', 'token', 'api_key'])}",
        "docker_secret_mount": f"RUN --mount=type=secret,id={env.lower()} cat /run/secrets/{env.lower()}",
    }
    form = r.choice(list(forms))
    return Item(r.choice(CRED_NAMES), forms[form], form)


def cat_masked(r: random.Random) -> Item:
    """Masked or redacted values.

    Sources:
    - [GL] global ``[allowlist] regexes``: ``^(?i:a+|b+|...|x+|y+|z+|\\*+|\\.+)$``.
    - [GL] rule ``curl-auth-user`` allowlist alternatives ``\\*+`` and ``x+`` for the password part.
    - [DS] ``is_not_alphanumeric_string``: "secrets should have at least ONE letter",
      to drop values like ``*****``.
    - Gitleaks' own ``--redact`` output (``gitleaks dir --help``, v8.30.1): ``REDACTED``,
      or a kept prefix when ``--redact=<percent>`` redacts only part of the value.
    Prefix-kept masks break the vendor charset or length on purpose, so they never
    match a full key shape.
    """
    n = r.randint(6, 24)
    forms = {
        "stars": "*" * n,
        "dots": "." * r.randint(3, 12),
        "xs": r.choice("xX") * n,
        "bullets": "•" * n,
        "redacted_word": r.choice(["REDACTED", "[REDACTED]", "<redacted>", "***REDACTED***", "redacted"]),
        "prefix_mask": r.choice(["AKIA", "sk-", "ghp_", "glpat-", "hf_", "xoxb-", "sk-ant-", "AIza"])
        + _rs(r, UPPER_DIGITS, r.randint(0, 4)) + r.choice(["*" * r.randint(4, 16), "...", "…", "****...****"]),
        "prefix_suffix": r.choice(["sk-", "ghp_", ""]) + "*" * r.randint(4, 20) + _rs(r, ALNUM, 4),
        "userinfo_masked": f"{r.choice(['admin', 'app', 'svc'])}:{'*' * r.randint(4, 12)}",
    }
    form = r.choice(list(forms))
    return Item(r.choice(CRED_NAMES), forms[form], form)


def _ssh_string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _pem(kind: str, der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {kind}-----\n" + "\n".join(lines) + f"\n-----END {kind}-----"


# DER prefix of an Ed25519 SubjectPublicKeyInfo (RFC 8410 OID 1.3.101.112).
_ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")


def cat_public_key(r: random.Random) -> Item:
    """Public keys and certificates (public by design).

    Sources:
    - [GL] rule ``private-key`` regex ``-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----``:
      only PRIVATE KEY blocks are secrets, so PUBLIC KEY and CERTIFICATE blocks are lookalikes.
    - [GL] rule ``generic-api-key`` allowlist ``pub(?:lic)?[_.-]?key`` and ``public[_.-]?token``.
    The key material is random bytes inside the real wire format (OpenSSH public key
    blob, SPKI DER, a DER SEQUENCE header for certificates). No private half exists.
    """
    k = r.random()
    if k < 0.35:
        kind = r.choice(["ssh-ed25519", "ssh-rsa"])
        if kind == "ssh-ed25519":
            blob = _ssh_string(b"ssh-ed25519") + _ssh_string(r.randbytes(32))
        else:
            n = b"\x00\xc1" + r.randbytes(255)
            blob = _ssh_string(b"ssh-rsa") + _ssh_string(b"\x01\x00\x01") + _ssh_string(n)
        comment = r.choice(["deploy@build-01", "ci@runner", "laptop", "ops@bastion", ""])
        value = f"{kind} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")
        return Item(r.choice(["ssh_public_key", "authorized_key", "public_key", "pubkey", "deploy_key_pub"]), value, "ssh_pub")
    if k < 0.65:
        value = _pem("PUBLIC KEY", _ED25519_SPKI + r.randbytes(32))
        return Item(r.choice(["public_key", "signing_public_key", "jwks_public_key", "verify_key_pem"]), value, "pem_public", block=True, fname="public.pem")
    length = r.choice([700, 900, 1200])
    der = b"\x30\x82" + struct.pack(">H", length) + b"\x30\x82" + struct.pack(">H", length - 140) + r.randbytes(length - 4)
    value = _pem("CERTIFICATE", der)
    return Item(r.choice(["tls_cert", "ca_cert", "certificate", "server_cert", "ca_bundle"]), value, "pem_cert", block=True, fname="cert.pem")


def cat_hash_digest(r: random.Random) -> Item:
    """Hashes and digests, shown with the context that says what they are.

    Sources:
    - [GL] global ``[allowlist] paths``: ``go\\.(?:mod|sum|work(?:\\.sum)?)$`` (go.sum ``h1:`` hashes)
      and ``verification-metadata\\.xml`` (Gradle dependency checksums).
    - [GL] rule ``generic-api-key`` allowlist ``issuerkeyhash``.
    """
    data = r.randbytes(32)
    forms = {
        "git_commit": ("commit", hashlib.sha1(data).hexdigest()),
        "git_short": ("commit", hashlib.sha1(data).hexdigest()[: r.choice([7, 8, 12])]),
        "sha256_file": ("sha256", hashlib.sha256(data).hexdigest()),
        "md5_file": ("md5", hashlib.md5(data).hexdigest()),
        "image_digest": ("image_digest", "sha256:" + hashlib.sha256(data).hexdigest()),
        "image_ref": ("image", f"ghcr.io/example/{r.choice(['api', 'worker', 'web'])}@sha256:{hashlib.sha256(data).hexdigest()}"),
        "etag": ("etag", '"' + hashlib.md5(data).hexdigest() + '"'),
        "go_sum": ("go_sum", f"github.com/example/{r.choice(['log', 'cfg', 'retry'])} v1.{r.randint(0, 9)}.{r.randint(0, 20)} h1:"
                   + base64.b64encode(hashlib.sha256(data).digest()).decode()),
        "gradle_sha256": ("sha256", f'<sha256 value="{hashlib.sha256(data).hexdigest()}" origin="Generated by Gradle"/>'),
        "issuer_key_hash": ("issuerKeyHash", hashlib.sha1(data).hexdigest()),
        "content_hash": ("content_hash", hashlib.sha256(data).hexdigest()[:r.choice([16, 32, 64])]),
    }
    form = r.choice(list(forms))
    key, value = forms[form]
    key = {"commit": r.choice(["commit", "git_sha", "revision", "head_sha"]),
           "sha256": r.choice(["sha256", "checksum", "sha256sum", "file_hash"]),
           "md5": r.choice(["md5", "md5sum", "content_md5"])}.get(key, key)
    return Item(key, value, form)


def _uuid(r: random.Random) -> str:
    h = list(f"{r.getrandbits(128):032x}")
    h[12] = "4"
    h[16] = "89ab"[int(h[16], 16) % 4]
    s = "".join(h)
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


def cat_uuid_id(r: random.Random) -> Item:
    """UUIDs and identifiers.

    Sources:
    - [DS] ``is_potential_uuid`` (regex ``[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}``)
      and ``is_likely_id_string`` (key regex ``(^(id|myid|userid)|_id)s?[^a-z0-9]``).
    - [GL] global ``stopwords`` (a UUID, ``014df517-...``) and rule ``generic-api-key``
      allowlist names ``access[_.-]?id``, ``api[_.-]?(?:id|name|version)``,
      ``credentials?[_.-]?id``, ``key[_.-]?(?:alias|id|name)``, ``UserSecretsId``.
    Key names stay on the identifier side (``user_id``, ``trace_id``), never session or
    bearer names, because some vendors issue UUIDs as real tokens.
    """
    forms = {
        "uuid": lambda: _uuid(r),
        "uuid_upper": lambda: _uuid(r).upper(),
        "uuid_braced": lambda: "{" + _uuid(r).upper() + "}",
        "hex_id": lambda: _rs(r, HEX, r.choice([24, 32])),
        "numeric_id": lambda: str(r.randint(10**8, 10**18)),
        "prefixed_id": lambda: r.choice(["usr_", "ord_", "evt_", "job_", "inv_", "prj_"]) + _rs(r, ALNUM, r.choice([14, 20, 24])),
        "ulid": lambda: _rs(r, "0123456789ABCDEFGHJKMNPQRSTVWXYZ", 26),
        "trace_id": lambda: _rs(r, HEX, 32),
    }
    form = r.choice(list(forms))
    key = r.choice([
        "id", "user_id", "userId", "order_id", "trace_id", "span_id", "request_id", "correlation_id",
        "tenant_id", "project_id", "workspace_id", "device_id", "job_id", "UserSecretsId",
        "credentials_id", "key_id", "api_id", "access_id", "invoice_id", "subscription_id",
    ])
    if form == "trace_id":
        key = r.choice(["trace_id", "traceId", "X-B3-TraceId"])
    return Item(key, forms[form](), form)


_SEQUENCES = [
    string.ascii_uppercase + string.ascii_uppercase + string.digits + "+/",
    string.digits + string.ascii_uppercase + string.ascii_uppercase + "+/",
    (string.digits + string.ascii_uppercase) * 2,
    string.digits * 2,
    string.hexdigits.upper() + string.hexdigits.upper(),
    string.ascii_uppercase + "=/",
]


def cat_sequential(r: random.Random) -> Item:
    """Sequential or repeated strings.

    Sources:
    - [DS] ``is_sequential_string``: substrings of the base64 alphabet, digits, hex digits.
    - [GL] global ``[allowlist] regexes`` ``^(?i:a+|b+|...|z+|\\*+|\\.+)$`` (one repeated char)
      and global ``stopwords`` ``abcdefghijklmnopqrstuvwxyz``; rule ``generic-api-key``
      stopword ``000000``.
    A sequential value under a ``password`` key would still be a (weak) password, so
    this category uses neutral names only.
    """
    k = r.random()
    if k < 0.6:
        seq = r.choice(_SEQUENCES)
        n = r.randint(10, min(40, len(seq)))
        start = r.randint(0, len(seq) - n)
        value = seq[start : start + n]
        if r.random() < 0.5:
            value = value.lower()
        form = "sequential"
    elif k < 0.8:
        value = r.choice(string.ascii_letters + "0") * r.randint(8, 40)
        form = "repeated_char"
    else:
        value = r.choice(["abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789",
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", "000000"])
        form = "alphabet"
    key = r.choice(["alphabet", "charset", "sequence", "test_pattern", "row_labels", "column_order",
                    "fill", "padding", "base32_alphabet", "symbols", "sample_input", "keyboard_row"])
    return Item(key, value, form)


_PKG_PARTS = ["fast", "tiny", "json", "yaml", "http", "path", "color", "date", "retry", "glob",
              "parse", "string", "util", "stream", "cache", "async", "log", "event", "config", "token"]


def _pkg(r: random.Random) -> str:
    return r.choice(_PKG_PARTS) + r.choice(["-", ""]) + r.choice(_PKG_PARTS)


def _ver(r: random.Random) -> str:
    return f"{r.randint(0, 9)}.{r.randint(0, 30)}.{r.randint(0, 20)}"


def cat_lockfile(r: random.Random) -> Item:
    """Lock-file integrity hashes.

    Sources:
    - [GL] global ``[allowlist] paths``: ``(?:deno\\.lock|npm-shrinkwrap\\.json|package-lock\\.json|pnpm-lock\\.yaml|yarn\\.lock)$``,
      ``(?:Pipfile|poetry)\\.lock$``, ``gradle\\.lockfile$``, ``go\\.(?:mod|sum|work(?:\\.sum)?)$``.
    - [DS] ``is_lock_file``: package-lock.json, yarn.lock, Pipfile.lock, poetry.lock,
      Cargo.lock, composer.lock, Gemfile.lock, and others.
    The content is a short excerpt in each lock file's real layout.
    """
    def sha512() -> str:
        return "sha512-" + base64.b64encode(hashlib.sha512(r.randbytes(16)).digest()).decode()

    def sha256hex() -> str:
        return hashlib.sha256(r.randbytes(16)).hexdigest()

    kind = r.choice(["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Pipfile.lock", "poetry.lock", "Cargo.lock", "go.sum"])
    rows = []
    for _ in range(r.randint(1, 3)):
        p, v = _pkg(r), _ver(r)
        if kind == "package-lock.json":
            rows.append(f'    "node_modules/{p}": {{\n      "version": "{v}",\n'
                        f'      "resolved": "https://registry.npmjs.org/{p}/-/{p}-{v}.tgz",\n'
                        f'      "integrity": "{sha512()}"\n    }}')
        elif kind == "yarn.lock":
            rows.append(f'{p}@^{v}:\n  version "{v}"\n  resolved "https://registry.yarnpkg.com/{p}/-/{p}-{v}.tgz#'
                        f'{hashlib.sha1(r.randbytes(8)).hexdigest()}"\n  integrity {sha512()}')
        elif kind == "pnpm-lock.yaml":
            rows.append(f"  /{p}@{v}:\n    resolution: {{integrity: {sha512()}}}\n    dev: {r.choice(['true', 'false'])}")
        elif kind == "Pipfile.lock":
            rows.append(f'        "{p}": {{\n            "hashes": [\n                "sha256:{sha256hex()}",\n'
                        f'                "sha256:{sha256hex()}"\n            ],\n            "version": "=={v}"\n        }}')
        elif kind == "poetry.lock":
            rows.append(f'[[package]]\nname = "{p}"\nversion = "{v}"\nfiles = [\n'
                        f'    {{file = "{p}-{v}.tar.gz", hash = "sha256:{sha256hex()}"}},\n]')
        elif kind == "Cargo.lock":
            rows.append(f'[[package]]\nname = "{p}"\nversion = "{v}"\n'
                        f'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "{sha256hex()}"')
        else:
            h = base64.b64encode(hashlib.sha256(r.randbytes(16)).digest()).decode()
            rows.append(f"github.com/example/{p} v{v} h1:{h}")
    sep = ",\n" if kind in ("package-lock.json", "Pipfile.lock") else "\n"
    return Item("integrity", sep.join(rows), kind.replace(".", "_").replace("-", "_").lower(), block=True, fname=kind)


# 1x1 PNG header bytes; the IDAT payload after it is random.
_PNG_HEAD = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")


def cat_benign_base64(r: random.Random) -> Item:
    """Non-secret base64: data URIs and pagination cursors.

    Sources:
    - [RFC2397] ``data:[<mediatype>][;base64],<data>`` (section 3) and the
      ``data:image/gif;base64,...`` example (section 4).
    - [SLACK-PAG] "Cursor-based pagination": ``next_cursor`` values such as
      ``dXNlcjpVMEc5V0ZYTlo=``, which is base64 of ``user:<id>``.
    - [GL] global ``[allowlist] paths`` for image files ``(?i)\\.(?:bmp|gif|jpe?g|png|svg|tiff?)$``.
    """
    k = r.random()
    if k < 0.45:
        mt = r.choice(["image/png", "image/gif", "image/svg+xml", "text/plain", "application/json", "font/woff2"])
        if mt == "image/png":
            raw = _PNG_HEAD + r.randbytes(r.randint(24, 120))
        elif mt == "image/gif":
            raw = b"GIF89a\x01\x00\x01\x00" + r.randbytes(r.randint(20, 60))
        elif mt == "image/svg+xml":
            raw = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{r.randint(8, 64)}" height="{r.randint(8, 64)}">'
                   f'<circle cx="8" cy="8" r="{r.randint(2, 8)}" fill="#{_rs(r, HEX, 6)}"/></svg>').encode()
        elif mt == "text/plain":
            raw = r.choice(["Hello, World!", "A brief note", "sample text", "release notes v" + _ver(r)]).encode()
        elif mt == "application/json":
            raw = json.dumps({"theme": r.choice(["dark", "light"]), "lang": r.choice(["en", "fr", "de"])}).encode()
        else:
            raw = b"wOF2" + r.randbytes(r.randint(30, 80))
        value = f"data:{mt};base64,{base64.b64encode(raw).decode()}"
        key = r.choice(["src", "icon", "logo", "avatar", "thumbnail", "favicon", "image", "background"])
        return Item(key, value, "data_uri")
    if k < 0.8:
        payload = r.choice([
            f"user:U{_rs(r, UPPER_DIGITS, r.choice([8, 10]))}",
            f"channel:C{_rs(r, UPPER_DIGITS, 10)}",
            json.dumps({"offset": r.randint(0, 5000), "limit": r.choice([20, 50, 100])}, separators=(",", ":")),
            f"page={r.randint(2, 400)}",
            f"ts:{r.randint(1_600_000_000, 1_800_000_000)}.{r.randint(0, 999999):06d}",
        ])
        value = base64.b64encode(payload.encode()).decode()
        if r.random() < 0.3:
            value = value.rstrip("=")
        key = r.choice(["next_cursor", "cursor", "after", "before", "page_token", "nextPageToken", "continuation"])
        return Item(key, value, "cursor")
    payload = r.choice([
        json.dumps({"locale": r.choice(["en-US", "fr-FR"]), "tz": r.choice(["UTC", "Europe/Paris"])}),
        f"v=1;view={r.choice(['grid', 'list'])};sort={r.choice(['asc', 'desc'])}",
        r.choice(["the quick brown fox jumps over the lazy dog", "status ok", "build passed"]),
    ])
    return Item(r.choice(["state", "prefs", "view_state", "payload", "message_body"]),
                base64.b64encode(payload.encode()).decode(), "encoded_text")


@dataclass(frozen=True)
class Category:
    name: str
    gen: Callable[[random.Random], Item]
    cred_ok: bool  # may use credential-style key names and framing
    carriers: tuple[str, ...]


ALL_CARRIERS = ("tool_call_json", "tool_result_json", "shell", "env", "yaml", "toml", "log", "stack_trace",
                "http_headers", "connection_string", "ci_config", "prose", "chat")
_NO_CONN = tuple(c for c in ALL_CARRIERS if c != "connection_string")
# Lock files are whole files: env lines, headers and connection strings do not hold them.
_BLOCK_CARRIERS = ("tool_call_json", "tool_result_json", "shell", "yaml", "toml", "log", "stack_trace", "ci_config", "prose", "chat")

CATEGORIES: tuple[Category, ...] = (
    Category("placeholder", cat_placeholder, True, ALL_CARRIERS),
    Category("doc_example", cat_doc_example, True, _NO_CONN),
    Category("indirect_reference", cat_indirect_reference, True, ALL_CARRIERS),
    Category("masked", cat_masked, True, ALL_CARRIERS),
    Category("public_key", cat_public_key, False, _NO_CONN),
    Category("hash_digest", cat_hash_digest, False, _NO_CONN),
    Category("uuid_id", cat_uuid_id, False, _NO_CONN),
    Category("sequential", cat_sequential, False, _NO_CONN),
    Category("lockfile", cat_lockfile, False, _BLOCK_CARRIERS),
    Category("benign_base64", cat_benign_base64, False, _NO_CONN),
)
CATEGORY_BY_NAME = {c.name: c for c in CATEGORIES}

# ---------------------------------------------------------------------------
# carriers (same names as synth.CARRIERS)

FILLER = [("region", "us-east-1"), ("timeout", "30"), ("log_level", "info"), ("replicas", "3"),
          ("environment", "staging"), ("retries", "5"), ("port", "8080"), ("debug", "false")]
TOOLS = ["run_shell", "http_request", "write_file", "read_file", "query_db", "create_issue", "deploy_service", "fetch_page"]


def _ts(r: random.Random) -> str:
    return (f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}T{r.randint(0, 23):02d}:"
            f"{r.randint(0, 59):02d}:{r.randint(0, 59):02d}Z")


def _human(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").lower()


def _json_pairs(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in pairs)


def _pairs(r, items: list[Item], filler: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = [(it.key, it.value) for it in items] + [kv for kv in filler if kv[0] != "note"]
    r.shuffle(out)
    return [kv for kv in filler if kv[0] == "note"] + out  # a context note always comes first


def r_tool_call_json(r, items, filler, cred_ok):
    if items[0].block:
        it = items[0]
        return json.dumps({"type": "tool_use", "name": "write_file",
                           "input": {"path": it.fname, "content": it.value}}, ensure_ascii=False)
    return ('{"type": "tool_use", "name": "' + r.choice(TOOLS) + '", "input": {'
            + _json_pairs(_pairs(r, items, filler)) + "}}")


def r_tool_result_json(r, items, filler, cred_ok):
    tid = "toolu_" + _rs(r, ALNUM, 12)
    if items[0].block:
        it = items[0]
        return json.dumps({"tool_use_id": tid, "content": {"status": "ok", "path": it.fname, "text": it.value}}, ensure_ascii=False)
    return ('{"tool_use_id": "' + tid + '", "content": {"status": "ok", "data": {'
            + _json_pairs(_pairs(r, items, filler)) + "}}}")


def r_shell(r, items, filler, cred_ok):
    lines = []
    for it in items:
        if it.block:
            lines.append(f"$ {r.choice(['cat', 'head -n 40', 'less'])} {it.fname}\n{it.value}")
            continue
        s = r.random()
        if cred_ok and s < 0.25:
            lines.append(f"$ curl -s -H 'Authorization: Bearer {it.value}' https://api.example.com/v1/{r.choice(['models', 'items'])}")
        elif s < 0.55:
            lines.append(f"$ export {it.key.upper().replace('-', '_')}={it.value}")
        elif s < 0.8:
            lines.append(f"$ {r.choice(['./deploy.sh', 'python run.py', 'node app.js'])} --{it.key.lower().replace('_', '-')} {it.value}")
        else:
            lines.append(f"$ echo \"{it.key}: {it.value}\"\n{it.key}: {it.value}")
    return "\n".join(lines) + "\n" + r.choice(["ok\n", "done\n", "", "exit 0\n"])


def r_env(r, items, filler, cred_ok):
    out = r.choice(["# local settings\n", "", "NODE_ENV=production\n"])
    for k, v in _pairs(r, items, filler):
        q = '"' if "\n" in v or " " in v else r.choice(["", '"', "'"])
        out += f"{k.upper().replace('-', '_')}={q}{v}{q}\n"
    return out


def _yaml_scalar(v: str) -> str:
    if any(c in v for c in ":#{}[]&*!|>'\"%@`") or v.startswith(("-", "?", " ")) or v.lower() in ("true", "false", "null"):
        return json.dumps(v, ensure_ascii=False)
    return v


def r_yaml(r, items, filler, cred_ok):
    out = r.choice(["service:\n  name: api\n", "app:\n  replicas: 3\n"]) + r.choice(["  settings:\n", "  config:\n", "  data:\n"])
    for it in items:
        if it.block:
            out += f"    {it.key}: |\n      " + it.value.replace("\n", "\n      ") + "\n"
    for k, v in _pairs(r, [i for i in items if not i.block], filler):
        if "\n" in v:
            out += f"    {k}: |\n      " + v.replace("\n", "\n      ") + "\n"
        else:
            out += f"    {k}: {_yaml_scalar(v)}\n"
    return out


def r_toml(r, items, filler, cred_ok):
    if items[0].block and items[0].fname in ("poetry.lock", "Cargo.lock"):
        return f"# {items[0].fname}\n" + items[0].value + "\n"
    out = r.choice(["[server]\nport = 8080\n\n", "[tool.app]\ndebug = false\n\n"]) + r.choice(["[settings]\n", "[config]\n", "[app]\n"])
    for k, v in _pairs(r, items, filler):
        k = k.lower().replace("-", "_")
        out += f'{k} = """{v}"""\n' if "\n" in v else f"{k} = {json.dumps(v, ensure_ascii=False)}\n"
    return out


def r_log(r, items, filler, cred_ok):
    out = ""
    for it in items:
        lvl = r.choice(["INFO", "DEBUG", "WARN"])
        if it.block:
            out += f"{_ts(r)} {lvl} [{r.choice(['build', 'deps', 'tls', 'auth'])}] read {it.fname}:\n{it.value}\n"
        else:
            comp = r.choice(["auth", "http", "worker", "db", "config"])
            out += f"{_ts(r)} {lvl} [{comp}] {r.choice(['using', 'config', 'resolved'])} {it.key}={it.value}\n"
    for k, v in filler[:1]:
        out += f"{_ts(r)} INFO [config] {k}={v}\n"
    return out


def r_stack_trace(r, items, filler, cred_ok):
    out = f'Traceback (most recent call last):\n  File "/app/main.py", line {r.randint(10, 400)}, in handler\n'
    for it in items:
        if it.block:
            out += f"    data = parse({it.fname!r}, '''{it.value}''')\n"
        elif cred_ok:
            out += f"    client = Client({it.key.lower()}={it.value!r})\n"
        else:
            out += f"    record = lookup({it.key.lower()}={it.value!r})\n"
    return out + r.choice(["ConnectionError: timed out\n", "ValueError: invalid response\n", "KeyError: 'status'\n"])


def r_http_headers(r, items, filler, cred_ok):
    out = f"{r.choice(['GET', 'POST'])} /v1/{r.choice(['chat', 'items', 'repos'])} HTTP/1.1\nHost: api.example.com\n"
    for it in items:
        v = it.value.replace("\n", " ")
        if cred_ok:
            h = r.choice(["Authorization: Bearer ", "X-Api-Key: ", f"X-{it.key.title().replace('_', '-')}: "])
        else:
            h = f"X-{it.key[:1].upper() + it.key[1:].replace('_', '-')}: "
        out += h + v + "\n"
    return out + "Content-Type: application/json\n"


def r_connection_string(r, items, filler, cred_ok):
    out = ""
    for it in items:
        v = it.value
        s = r.random()
        if s < 0.4:
            out += f"Server=tcp:sql.example.net,1433;Database=app;User ID=svc;Password={v};Encrypt=True;\n"
        elif s < 0.7 and ":" not in v and "/" not in v and "@" not in v:
            user = r.choice(["app", "svc_api", "reader"])
            out += f"DATABASE_URL = postgres://{user}:{v}@db.example.net:5432/app\n"
        else:
            out += f"{it.key} = {v}\n"
    return out


def r_ci_config(r, items, filler, cred_ok):
    out = "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    env:\n"
    for k, v in _pairs(r, [i for i in items if not i.block], filler[:1]):
        out += f"      {k.upper().replace('-', '_')}: {_yaml_scalar(v.replace(chr(10), ' '))}\n"
    out += "    steps:\n"
    for it in items:
        if it.block:
            out += f"      - name: write {it.fname}\n        run: |\n          cat > {it.fname} <<'EOF'\n          " + it.value.replace("\n", "\n          ") + "\n          EOF\n"
    return out + "      - run: make test\n"


_WORDS = ("the build finished and the deploy step started on the staging cluster while the agent waited for "
          "the tool result then retried the request after the timeout and wrote a summary of the changes").split()


def _sentence(r) -> str:
    return " ".join(r.choice(_WORDS) for _ in range(r.randint(6, 14))).capitalize() + "."


def r_prose(r, items, filler, cred_ok):
    out = _sentence(r) + " "
    for it in items:
        if it.lead:
            out += it.lead + " "
        if it.block:
            out += f"Here is {it.fname}:\n{it.value}\n"
            continue
        lead = r.choice([f"The {_human(it.key)} is ", f"I set {it.key} to ", "Use this value: ",
                         f"Can you check why the {_human(it.key)} ", f"In the config, {it.key} = "])
        tail = r.choice([". ", " and it still fails. ", "\n", " in the config. "])
        out += lead + it.value + tail
    return out + _sentence(r)


def r_chat(r, items, filler, cred_ok):
    out = f"user: {_sentence(r)}\nassistant: Sure, can you share the details?\nuser: "
    for it in items:
        if it.lead:
            out += it.lead + " "
        if it.block:
            out += f"here is {it.fname}:\n{it.value}\n"
        else:
            out += f"{_human(it.key)}: {it.value}\n"
    return out + "assistant: Thanks, running the tool now.\n"


RENDERERS: dict[str, Callable] = {
    "tool_call_json": r_tool_call_json, "tool_result_json": r_tool_result_json, "shell": r_shell, "env": r_env,
    "yaml": r_yaml, "toml": r_toml, "log": r_log, "stack_trace": r_stack_trace, "http_headers": r_http_headers,
    "connection_string": r_connection_string, "ci_config": r_ci_config, "prose": r_prose, "chat": r_chat,
}

# ---------------------------------------------------------------------------
# public API


def make_lookalike(r: random.Random, idx: int, category: str | None = None) -> Example:
    """One all-negative document for one category, in one carrier."""
    cat = CATEGORY_BY_NAME[category] if category else r.choice(CATEGORIES)
    carrier = r.choice(cat.carriers)
    block_cat = cat.name == "lockfile"
    n_items = 1 if block_cat else r.choices([1, 2, 3], weights=[6, 3, 1])[0]
    items = [cat.gen(r) for _ in range(n_items)]
    if any(it.block for it in items):
        items = [next(it for it in items if it.block)]  # one block per document keeps carriers simple
    filler = r.sample(FILLER, r.randint(0, 2))
    leads = [it.lead for it in items if it.lead]
    if leads and carrier in ("tool_call_json", "tool_result_json"):
        filler = [("note", " ".join(leads))] + filler
    text = RENDERERS[carrier](r, items, filler, cat.cred_ok)
    if leads and carrier not in ("tool_call_json", "tool_result_json", "prose", "chat"):
        # prose and chat render leads inline; other text carriers get a comment line first
        text = "".join(f"# {lead}\n" for lead in leads) + text
    return Example(
        id=f"lookalike-{idx}", text=text, spans=[], carrier=carrier, source="synth",
        meta={"families": ["neg:lookalike_" + cat.name], "category": cat.name, "forms": [it.form for it in items]},
    )


def generate_lookalikes(n: int, seed: int = 0) -> list[Example]:
    """``n`` lookalike documents. Uses its own RNG stream so it never shifts synth.generate output."""
    r = random.Random(f"lookalikes-{seed}")
    return [make_lookalike(r, i) for i in range(n)]
