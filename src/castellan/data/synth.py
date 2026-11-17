"""Synthetic TRAINING data generator.

Every value is random with a valid-looking public structure (documented
prefixes and lengths). Nothing is a real credential.

Design choices:
- Training text holds secrets in plain form only. No splitting, encoding,
  homoglyphs or spelling out: the obfuscation ladder (eval only) measures
  whether the probe generalizes, so we never teach those transforms here.
- Hard negatives (UUIDs, git SHAs, hashes, documented example keys,
  placeholders, test fixtures, benign base64) appear with no spans, in the
  same carriers as real secrets.
- Text is assembled piece by piece so span offsets are exact, including
  inside JSON where values are escaped (the span covers the escaped form,
  which is what the model sees).

This module must never import or read eval_data/.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import uuid
from dataclasses import dataclass, field
from typing import Callable

from castellan.data.schema import Example, Span

ALNUM = string.ascii_letters + string.digits
UPPER_DIGITS = string.ascii_uppercase + string.digits
B32 = string.ascii_uppercase + "234567"
B64 = ALNUM + "+/"
B64URL = ALNUM + "-_"
HEX = "0123456789abcdef"


def rs(r: random.Random, alphabet: str, n: int) -> str:
    return "".join(r.choice(alphabet) for _ in range(n))


# secret value generators: each returns (value, label, family)

def aws_key_id(r):
    return r.choice(["AKIA", "ASIA"]) + rs(r, B32, 16), "SECRET_CLOUD_KEY", "aws_key_id"


def aws_secret(r):
    return rs(r, B64, 40), "SECRET_CLOUD_KEY", "aws_secret"


def gcp_api_key(r):
    return "AIza" + rs(r, B64URL, 35), "SECRET_CLOUD_KEY", "gcp_api_key"


def azure_storage_key(r):
    return base64.b64encode(r.randbytes(64)).decode(), "SECRET_CLOUD_KEY", "azure_storage_key"


def openai_key(r):
    if r.random() < 0.5:
        return "sk-" + rs(r, ALNUM, 48), "SECRET_AI_KEY", "openai_legacy"
    return "sk-proj-" + rs(r, B64URL, r.randint(100, 156)), "SECRET_AI_KEY", "openai_project"


def anthropic_key(r):
    return "sk-ant-api03-" + rs(r, B64URL, 93) + "AA", "SECRET_AI_KEY", "anthropic"


def hf_token(r):
    return "hf_" + rs(r, string.ascii_letters, 34), "SECRET_AI_KEY", "huggingface"


def github_token(r):
    if r.random() < 0.25:
        return "github_pat_" + rs(r, ALNUM, 22) + "_" + rs(r, ALNUM, 59), "SECRET_VCS_TOKEN", "github_fine"
    return r.choice(["ghp_", "gho_", "ghs_", "ghu_"]) + rs(r, ALNUM, 36), "SECRET_VCS_TOKEN", "github_classic"


def gitlab_token(r):
    return "glpat-" + rs(r, B64URL, 20), "SECRET_VCS_TOKEN", "gitlab"


def _b64url_json(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def jwt(r):
    head = _b64url_json({"alg": r.choice(["HS256", "RS256", "ES256"]), "typ": "JWT"})
    body = _b64url_json({
        "sub": str(r.randint(10**5, 10**9)),
        "iat": r.randint(1_600_000_000, 1_800_000_000),
        "scope": r.choice(["read", "admin", "user write"]),
    })
    return f"{head}.{body}.{rs(r, B64URL, r.choice([43, 86, 342]))}", "SECRET_JWT", "jwt"


def private_key(r):
    kind = r.choice(["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"])
    body = base64.b64encode(r.randbytes(r.choice([120, 240, 600]))).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {kind}-----\n" + "\n".join(lines) + f"\n-----END {kind}-----", "SECRET_PRIVATE_KEY", "pem"


def db_url(r):
    scheme = r.choice(["postgres", "postgresql", "mysql", "mongodb+srv", "redis", "amqp"])
    user = r.choice(["app", "admin", "svc_api", "root", "etl", "reader"])
    pw = rs(r, ALNUM + "!*-_", r.randint(12, 28))
    host = r.choice(["db", "prod-db", "cluster0.ab1cd", "10.0.3.12", "pg-main.internal"]) + r.choice(
        ["", ".example.net", ".svc.cluster.local"]
    )
    port = {"postgres": 5432, "postgresql": 5432, "mysql": 3306, "redis": 6379, "amqp": 5672}.get(scheme)
    hp = host if port is None or r.random() < 0.3 else f"{host}:{port}"
    return f"{scheme}://{user}:{pw}@{hp}/{r.choice(['app', 'main', 'orders', '0', 'prod'])}", "SECRET_DB_URL", "db_url"


def generic_secret(r):
    kind = r.random()
    if kind < 0.25:
        v = "xox" + r.choice("bpas") + "-" + "-".join(str(r.randint(10**9, 10**12)) for _ in range(2)) + "-" + rs(r, ALNUM, 24)
    elif kind < 0.45:
        v = r.choice(["sk_live_", "rk_live_"]) + rs(r, ALNUM, r.choice([24, 99]))
    elif kind < 0.65:
        v = rs(r, HEX, r.choice([32, 40, 64]))
    elif kind < 0.85:
        v = rs(r, ALNUM, r.randint(24, 48))
    else:  # human-ish password
        v = rs(r, string.ascii_letters, r.randint(5, 9)) + rs(r, string.digits, r.randint(2, 4)) + r.choice("!@#$%&*")
    return v, "SECRET_GENERIC", "generic"


SECRET_GENS: list[tuple[Callable, float]] = [
    (aws_key_id, 1.0), (aws_secret, 0.7), (gcp_api_key, 0.8), (azure_storage_key, 0.5),
    (openai_key, 1.0), (anthropic_key, 0.8), (hf_token, 0.6),
    (github_token, 1.0), (gitlab_token, 0.6),
    (jwt, 1.0), (private_key, 0.7), (db_url, 1.0), (generic_secret, 1.5),
]

# variable names that suit each family
KEY_NAMES = {
    "SECRET_CLOUD_KEY": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "aws_secret", "GOOGLE_API_KEY", "gcp_key", "AZURE_STORAGE_KEY", "storage_account_key"],
    "SECRET_AI_KEY": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN", "llm_api_key", "api_key", "model_token"],
    "SECRET_VCS_TOKEN": ["GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "git_token", "CI_JOB_TOKEN", "token"],
    "SECRET_JWT": ["Authorization", "access_token", "id_token", "session_jwt", "bearer"],
    "SECRET_PRIVATE_KEY": ["SSH_PRIVATE_KEY", "private_key", "tls_key", "signing_key"],
    "SECRET_DB_URL": ["DATABASE_URL", "db_url", "MONGO_URI", "REDIS_URL", "connection_string", "dsn"],
    "SECRET_GENERIC": ["SLACK_BOT_TOKEN", "STRIPE_SECRET_KEY", "password", "secret", "api_secret", "client_secret", "DB_PASSWORD", "webhook_secret"],
}

# PII generators
FIRST = ["Maria", "James", "Wei", "Aisha", "Carlos", "Priya", "Olga", "Kenji", "Fatima", "Liam", "Noor", "Sofia", "Daniel", "Amara", "Lucas", "Mei"]
LAST = ["Garcia", "Smith", "Chen", "Okafor", "Silva", "Patel", "Ivanova", "Tanaka", "Haddad", "Murphy", "Khan", "Rossi", "Cohen", "Mensah", "Novak", "Lin"]
STREETS = ["Oak St", "Maple Ave", "Harbor Rd", "Elm Street", "Cedar Ln", "Pine Ct", "Lakeview Dr", "Mill Road"]
CITIES = ["Springfield, IL 62704", "Portland, OR 97205", "Austin, TX 78701", "Leeds LS1 4AP", "Toronto, ON M5V 2T6", "Denver, CO 80202"]
DOMAINS = ["example.com", "mail.example.org", "corp.example.net", "example.io"]


def person(r):
    return f"{r.choice(FIRST)} {r.choice(LAST)}", "PERSON", "person"


def email(r):
    f, l = r.choice(FIRST).lower(), r.choice(LAST).lower()
    local = r.choice([f"{f}.{l}", f"{f[0]}{l}", f"{f}{r.randint(1, 99)}", f"{l}.{f}"])
    return f"{local}@{r.choice(DOMAINS)}", "EMAIL", "email"


def phone(r):
    fmt = r.random()
    if fmt < 0.4:
        return f"({r.randint(201, 989)}) {r.randint(200, 999)}-{r.randint(0, 9999):04d}", "PHONE", "phone"
    if fmt < 0.7:
        return f"+1 {r.randint(201, 989)}-{r.randint(200, 999)}-{r.randint(0, 9999):04d}", "PHONE", "phone"
    return f"+44 7{r.randint(100, 999)} {r.randint(100000, 999999)}", "PHONE", "phone"


def _luhn_complete(digits: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return digits + str((10 - total % 10) % 10)


def card(r):
    prefix = r.choice(["4", "51", "52", "55", "37", "6011"])
    n = 15 if prefix == "37" else 16
    num = _luhn_complete(prefix + rs(r, string.digits, n - len(prefix) - 1))
    if r.random() < 0.6:
        groups = [num[i : i + 4] for i in range(0, len(num), 4)]
        num = r.choice([" ", "-"]).join(groups)
    return num, "CARD", "card"


def gov_id(r):
    return f"{r.randint(100, 665):03d}-{r.randint(1, 99):02d}-{r.randint(1, 9999):04d}", "GOV_ID", "ssn"


def ip_address(r):
    return f"{r.choice([23, 45, 66, 81, 104, 152, 185, 203])}.{r.randint(0, 255)}.{r.randint(0, 255)}.{r.randint(1, 254)}", "IP_ADDRESS", "ipv4"


def address(r):
    return f"{r.randint(1, 9999)} {r.choice(STREETS)}, {r.choice(CITIES)}", "ADDRESS", "address"


PII_GENS: list[tuple[Callable, float]] = [
    (person, 1.0), (email, 1.0), (phone, 0.8), (card, 0.6), (gov_id, 0.5), (ip_address, 0.6), (address, 0.6),
]

# hard negatives: (value, kind). These get no span.

AWS_DOC_EXAMPLES = ["AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"]


def hard_negative(r) -> tuple[str, str]:
    k = r.random()
    if k < 0.15:
        return str(uuid.UUID(int=r.getrandbits(128), version=4)), "uuid"
    if k < 0.28:
        return rs(r, HEX, 40), "git_sha"
    if k < 0.36:
        return rs(r, HEX, r.choice([7, 12])), "short_sha"
    if k < 0.46:
        return hashlib.sha256(r.randbytes(16)).hexdigest(), "sha256"
    if k < 0.52:
        return r.choice(AWS_DOC_EXAMPLES), "doc_example"
    if k < 0.66:
        return r.choice([
            "<YOUR_API_KEY>", "your-api-key-here", "sk-...", "sk-xxxxxxxxxxxxxxxxxxxxxxxx", "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "${OPENAI_API_KEY}", "$GITHUB_TOKEN", "{{ secrets.API_KEY }}", "changeme", "REDACTED", "********",
            "<redacted>", "None", "null", "TODO", "replace_me", "os.environ['API_KEY']", "process.env.DB_PASSWORD",
        ]), "placeholder"
    if k < 0.74:
        return r.choice(["test_key_123", "dummy-token", "fake_secret_for_tests", "sk_test_" + rs(r, "x", 24),
                         "password123", "testpassword", "AKIA" + "X" * 16, "localhost-dev-key"]), "test_fixture"
    if k < 0.84:
        text = r.choice(["hello world", "the quick brown fox", "user preferences saved", "{\"theme\":\"dark\"}", "lorem ipsum dolor"])
        return base64.b64encode(text.encode()).decode(), "benign_base64"
    if k < 0.90:
        return f"req_{rs(r, ALNUM, 16)}" if r.random() < 0.5 else f"msg_{rs(r, ALNUM, 24)}", "request_id"
    if k < 0.95:
        return f"sha256:{hashlib.sha256(r.randbytes(8)).hexdigest()}", "image_digest"
    return f"{r.randint(0, 9)}.{r.randint(0, 30)}.{r.randint(0, 99)}", "version"


# text assembly with exact offsets


@dataclass
class Builder:
    parts: list[str] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    n: int = 0

    def add(self, s: str) -> "Builder":
        self.parts.append(s)
        self.n += len(s)
        return self

    def value(self, v: str, label: str | None, json_escape: bool = False) -> "Builder":
        s = json.dumps(v)[1:-1] if json_escape else v
        if label is not None:
            self.spans.append(Span(self.n, self.n + len(s), label))
        return self.add(s)

    def text(self) -> str:
        return "".join(self.parts)


Item = tuple[str, str | None, str]  # (value, label or None for hard negative, key name)

BENIGN_WORDS = (
    "the build finished and the deploy step started on the staging cluster while the agent waited for "
    "the tool result then retried the request after the timeout and wrote a summary of the changes"
).split()
TOOLS = ["run_shell", "http_request", "write_file", "read_file", "query_db", "send_email", "create_issue", "deploy_service"]


def _sentence(r, n=(6, 16)) -> str:
    return " ".join(r.choice(BENIGN_WORDS) for _ in range(r.randint(*n))).capitalize() + "."


def _key_for(r, label: str | None) -> str:
    if label is None:
        return r.choice(sum(KEY_NAMES.values(), []) + ["request_id", "commit", "checksum", "image", "version", "trace_id"])
    if label in KEY_NAMES:
        return r.choice(KEY_NAMES[label])
    return {"EMAIL": "email", "PHONE": "phone", "CARD": "card_number", "GOV_ID": "ssn", "IP_ADDRESS": "client_ip",
            "PERSON": "name", "ADDRESS": "address"}.get(label, "value")


# carriers. Each takes (r, items) and returns a Builder.

def c_tool_call_json(r, items):
    b = Builder().add('{"type": "tool_use", "name": "' + r.choice(TOOLS) + '", "input": {')
    for i, (v, lab, key) in enumerate(items):
        b.add(("" if i == 0 else ", ") + f'"{key}": "').value(v, lab, json_escape=True).add('"')
    return b.add(', "timeout": ' + str(r.choice([10, 30, 60])) + "}}")


def c_tool_result_json(r, items):
    b = Builder().add('{"tool_use_id": "toolu_' + rs(r, ALNUM, 12) + '", "content": {"status": "ok", "data": {')
    for i, (v, lab, key) in enumerate(items):
        b.add(("" if i == 0 else ", ") + f'"{key}": "').value(v, lab, json_escape=True).add('"')
    return b.add("}}}")


def c_shell(r, items):
    b = Builder()
    for v, lab, key in items:
        if "\n" in v:
            b.add(f"cat > key.pem <<'EOF'\n").value(v, lab).add("\nEOF\n")
            continue
        style = r.random()
        if style < 0.4:
            b.add(f"$ export {key.upper()}=").value(v, lab).add("\n")
        elif style < 0.7:
            b.add(f"$ curl -s -H 'Authorization: Bearer ").value(v, lab).add(f"' https://api.example.com/v1/{r.choice(['models', 'items', 'runs'])}\n")
        else:
            b.add(f"$ {r.choice(['./deploy.sh', 'python run.py', 'node app.js'])} --{key.lower().replace('_', '-')} ").value(v, lab).add("\n")
    return b.add(r.choice(["ok\n", "done\n", "", "exit 0\n"]))


def c_env(r, items):
    b = Builder().add(r.choice(["# local settings\n", "", "NODE_ENV=production\n"]))
    for v, lab, key in items:
        q = r.choice(["", '"', "'"]) if "\n" not in v else '"'
        b.add(f"{key.upper()}={q}").value(v, lab).add(f"{q}\n")
    return b.add(r.choice(["LOG_LEVEL=info\n", "PORT=8080\n", ""]))


def c_yaml(r, items):
    b = Builder().add(r.choice(["service:\n  name: api\n", "app:\n  replicas: 3\n"])).add("  credentials:\n")
    for v, lab, key in items:
        if "\n" in v:
            # One span over the whole block, including the YAML indentation
            # between lines, to match how other carriers label PEM blocks.
            b.add(f"    {key}: |\n      ").value(v.replace("\n", "\n      "), lab).add("\n")
        else:
            b.add(f"    {key}: ").value(v, lab).add("\n")
    return b


def c_toml(r, items):
    b = Builder().add(r.choice(["[server]\nport = 8080\n\n", "[tool.app]\ndebug = false\n\n"])).add("[secrets]\n")
    for v, lab, key in items:
        if "\n" in v:
            b.add(f'{key.lower()} = """').value(v, lab).add('"""\n')
        else:
            b.add(f'{key.lower()} = "').value(v, lab).add('"\n')
    return b


def c_log(r, items):
    b = Builder()
    for v, lab, key in items:
        ts = f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}T{r.randint(0, 23):02d}:{r.randint(0, 59):02d}:{r.randint(0, 59):02d}Z"
        lvl = r.choice(["INFO", "DEBUG", "WARN", "ERROR"])
        if "\n" in v:
            b.add(f"{ts} {lvl} loaded key material:\n").value(v, lab).add("\n")
        else:
            b.add(f"{ts} {lvl} [{r.choice(['auth', 'http', 'worker', 'db'])}] {r.choice(['using', 'config', 'request with'])} {key}=").value(v, lab).add("\n")
    return b


def c_stack_trace(r, items):
    b = Builder().add("Traceback (most recent call last):\n  File \"/app/main.py\", line " + str(r.randint(10, 400)) + ", in handler\n")
    for v, lab, key in items:
        if "\n" in v:
            b.add("    key = load_key('''").value(v, lab).add("''')\n")
        else:
            b.add(f"    client = Client({key.lower()}='").value(v, lab).add("')\n")
    return b.add(r.choice(["ConnectionError: timed out\n", "ValueError: invalid response\n", "PermissionError: 403 Forbidden\n"]))


def c_http_headers(r, items):
    b = Builder().add(f"{r.choice(['GET', 'POST'])} /v1/{r.choice(['chat', 'items', 'repos'])} HTTP/1.1\nHost: api.example.com\n")
    for v, lab, key in items:
        if "\n" in v:
            b.add("X-Client-Key: ").value(v.replace("\n", " "), lab).add("\n")
            continue
        h = r.choice(["Authorization: Bearer ", "X-Api-Key: ", "Cookie: session=", f"X-{key.title().replace('_', '-')}: "])
        b.add(h).value(v, lab).add("\n")
    return b.add("Content-Type: application/json\n")


def c_connection_string(r, items):
    b = Builder()
    for v, lab, key in items:
        if lab == "SECRET_DB_URL" or "\n" in v:
            b.add(f"{key} = ").value(v.replace("\n", " ") if "\n" in v else v, lab).add("\n")
        else:
            b.add(f"Server=tcp:sql.example.net,1433;Database=app;User ID=svc;Password=").value(v, lab).add(";Encrypt=True;\n")
    return b


def c_ci_config(r, items):
    b = Builder().add("name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    env:\n")
    for v, lab, key in items:
        b.add(f"      {key.upper()}: ").value(v.replace("\n", "\\n"), lab).add("\n")
    return b.add("    steps:\n      - run: make test\n")


def c_prose(r, items):
    b = Builder().add(_sentence(r) + " ")
    for v, lab, key in items:
        lead = r.choice([
            f"The {key.lower().replace('_', ' ')} is ", "Use this: ", "Here it is ", "I pasted it below\n",
            f"My {key.lower().replace('_', ' ')} for the account is ", "Can you check why ",
        ])
        tail = r.choice([". ", " and it still fails. ", " so please keep it safe. ", "\n", " doesn't work. "])
        b.add(lead).value(v, lab).add(tail)
    return b.add(_sentence(r))


def c_chat(r, items):
    b = Builder().add(f"user: {_sentence(r)}\nassistant: Sure, can you share the details?\nuser: ")
    for v, lab, key in items:
        b.add(f"{key.lower().replace('_', ' ')}: ").value(v, lab).add("\n")
    return b.add("assistant: Thanks, running the tool now.\n")


CARRIERS: dict[str, Callable] = {
    "tool_call_json": c_tool_call_json, "tool_result_json": c_tool_result_json, "shell": c_shell, "env": c_env,
    "yaml": c_yaml, "toml": c_toml, "log": c_log, "stack_trace": c_stack_trace, "http_headers": c_http_headers,
    "connection_string": c_connection_string, "ci_config": c_ci_config, "prose": c_prose, "chat": c_chat,
}


def _weighted(r, gens):
    return r.choices([g for g, _ in gens], weights=[w for _, w in gens], k=1)[0]


def make_example(r: random.Random, idx: int, p_secret: float = 0.55, p_pii: float = 0.2) -> Example:
    """One example: secrets, PII or only hard negatives / benign values, in a random carrier."""
    carrier = r.choice(list(CARRIERS))
    items: list[Item] = []
    kind = r.random()
    families: list[str] = []
    if kind < p_secret:
        for _ in range(r.choices([1, 2, 3], weights=[6, 3, 1])[0]):
            v, lab, fam = _weighted(r, SECRET_GENS)(r)
            items.append((v, lab, _key_for(r, lab)))
            families.append(fam)
    elif kind < p_secret + p_pii:
        for _ in range(r.choices([1, 2, 3], weights=[4, 4, 2])[0]):
            v, lab, fam = _weighted(r, PII_GENS)(r)
            items.append((v, lab, _key_for(r, lab)))
            families.append(fam)
    # hard negatives: mixed into positives sometimes, alone otherwise
    n_neg = r.choice([0, 0, 1]) if items else r.choice([1, 1, 2, 3])
    for _ in range(n_neg):
        v, k = hard_negative(r)
        items.append((v, None, _key_for(r, None)))
        families.append("neg:" + k)
    if not items:
        items.append((rs(r, string.ascii_lowercase, 8), None, "name"))
    order = list(range(len(items)))
    r.shuffle(order)
    items = [items[i] for i in order]
    families = [families[i] for i in order] if families else []
    b = CARRIERS[carrier](r, items)
    return Example(
        id=f"synth-{idx}", text=b.text(), spans=b.spans, carrier=carrier, source="synth",
        meta={"families": families},
    )


def generate(n: int, seed: int = 0, **kw) -> list[Example]:
    r = random.Random(seed)
    return [make_example(r, i, **kw) for i in range(n)]
