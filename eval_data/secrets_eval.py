"""Frozen eval set generator: secrets_eval, dev, final, hardneg and longctx.

Run from the repo root:

    python eval_data/secrets_eval.py --out eval_data

Everything here is synthetic and deterministic. Each set has its own seed, so
two runs write byte-identical files. Secrets follow the public, documented
shapes of real credential formats (prefixes, lengths, alphabets) but the
bodies are random, so none of them is a working credential.

This module was written without reading the training generator. Training code
must never import it (CLAUDE.md section 8). ``ladder.py`` imports it for the
secret families and the value helpers.

The script never prints values. Self-checks report counts only.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from castellan.data.schema import Example, Span, write_jsonl  # noqa: E402

SOURCE = "eval:secrets_eval"

B62 = string.ascii_letters + string.digits
B64 = B62 + "+/"
B64URL = B62 + "-_"
HEX = "0123456789abcdef"
B32 = string.ascii_uppercase + "234567"
LOWER_DIGITS = string.ascii_lowercase + string.digits


def rs(rng: random.Random, n: int, alphabet: str) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --------------------------------------------------------------------------
# Items: one sensitive value (or one benign lookalike) plus how to name it
# --------------------------------------------------------------------------


@dataclass
class Item:
    value: str
    label: str | None  # None means benign filler or lookalike
    family: str
    key: str  # canonical UPPER_SNAKE identifier
    desc: str  # short human description used in prose

    @property
    def multiline(self) -> bool:
        return "\n" in self.value


# ---- secret families. Each returns a list of Items (an AWS pair returns two).


def aws_pair(rng: random.Random) -> list[Item]:
    akid = rng.choice(["AKIA", "ASIA"]) + rs(rng, 16, B32)
    secret = rs(rng, 40, B64)
    return [
        Item(akid, "SECRET_CLOUD_KEY", "aws_access_key_id", "AWS_ACCESS_KEY_ID", "AWS access key id"),
        Item(secret, "SECRET_CLOUD_KEY", "aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "AWS secret key"),
    ]


def gcp_api_key(rng):
    return [Item("AIza" + rs(rng, 35, B64URL), "SECRET_CLOUD_KEY", "gcp_api_key",
                 rng.choice(["GOOGLE_MAPS_API_KEY", "GCP_API_KEY", "FIREBASE_API_KEY"]), "Google Cloud API key")]


def azure_storage_key(rng):
    return [Item(rs(rng, 86, B64) + "==", "SECRET_CLOUD_KEY", "azure_storage_key",
                 rng.choice(["AZURE_STORAGE_KEY", "AZURE_STORAGE_ACCOUNT_KEY"]), "Azure storage account key")]


def do_token(rng):
    return [Item("dop_v1_" + rs(rng, 64, HEX), "SECRET_CLOUD_KEY", "digitalocean_token",
                 "DIGITALOCEAN_TOKEN", "DigitalOcean token")]


def alibaba_key(rng):
    return [Item("LTAI" + rs(rng, 20, B62), "SECRET_CLOUD_KEY", "alibaba_access_key",
                 "ALIBABA_CLOUD_ACCESS_KEY_ID", "Alibaba Cloud access key")]


def cloudflare_token(rng):
    return [Item(rs(rng, 40, B64URL), "SECRET_CLOUD_KEY", "cloudflare_api_token",
                 "CLOUDFLARE_API_TOKEN", "Cloudflare API token")]


def openai_key(rng):
    v = "sk-" + rng.choice(["proj", "svcacct"]) + "-" + rs(rng, 64, B64URL) + "T3BlbkFJ" + rs(rng, 64, B64URL)
    return [Item(v, "SECRET_AI_KEY", "openai_key", "OPENAI_API_KEY", "OpenAI API key")]


def anthropic_key(rng):
    v = "sk-ant-api03-" + rs(rng, 93, B64URL) + "AA"
    return [Item(v, "SECRET_AI_KEY", "anthropic_key", "ANTHROPIC_API_KEY", "Anthropic API key")]


def hf_token(rng):
    return [Item("hf_" + rs(rng, 34, string.ascii_letters), "SECRET_AI_KEY", "huggingface_token",
                 rng.choice(["HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"]), "Hugging Face token")]


def groq_key(rng):
    return [Item("gsk_" + rs(rng, 52, B62), "SECRET_AI_KEY", "groq_key", "GROQ_API_KEY", "Groq API key")]


def replicate_token(rng):
    return [Item("r8_" + rs(rng, 37, B62), "SECRET_AI_KEY", "replicate_token",
                 "REPLICATE_API_TOKEN", "Replicate API token")]


def perplexity_key(rng):
    return [Item("pplx-" + rs(rng, 48, B62), "SECRET_AI_KEY", "perplexity_key",
                 "PERPLEXITY_API_KEY", "Perplexity API key")]


def mistral_key(rng):
    return [Item(rs(rng, 32, B62), "SECRET_AI_KEY", "mistral_key", "MISTRAL_API_KEY", "Mistral API key")]


def github_classic(rng):
    pfx = rng.choice(["ghp_", "gho_", "ghs_", "ghu_"])
    return [Item(pfx + rs(rng, 36, B62), "SECRET_VCS_TOKEN", "github_token",
                 rng.choice(["GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"] if pfx == "ghp_" else ["GITHUB_TOKEN", "GH_TOKEN"]),
                 "GitHub token")]


def github_fine_grained(rng):
    v = "github_pat_11" + rs(rng, 20, string.ascii_uppercase + string.digits) + "_" + rs(rng, 59, B62)
    return [Item(v, "SECRET_VCS_TOKEN", "github_fine_grained_pat", "GITHUB_PAT", "GitHub fine-grained token")]


def gitlab_pat(rng):
    return [Item("glpat-" + rs(rng, 20, B64URL), "SECRET_VCS_TOKEN", "gitlab_pat",
                 rng.choice(["GITLAB_TOKEN", "CI_JOB_PUSH_TOKEN", "GL_TOKEN"]), "GitLab token")]


def gitlab_trigger(rng):
    return [Item("glptt-" + rs(rng, 40, HEX), "SECRET_VCS_TOKEN", "gitlab_trigger_token",
                 "GITLAB_TRIGGER_TOKEN", "GitLab pipeline trigger token")]


def bitbucket_app_password(rng):
    return [Item("ATBB" + rs(rng, 32, B62), "SECRET_VCS_TOKEN", "bitbucket_app_password",
                 "BITBUCKET_APP_PASSWORD", "Bitbucket app password")]


def jwt(rng):
    alg = rng.choice(["HS256", "RS256", "ES256"])
    header: dict = {"alg": alg, "typ": "JWT"}
    if rng.random() < 0.5:
        header["kid"] = rs(rng, 16, HEX)
    iat = rng.randint(1_750_000_000, 1_800_000_000)
    payload = {
        "sub": rng.choice(["svc-", "usr_", "client:"]) + rs(rng, 10, HEX),
        "iss": rng.choice(["https://auth.example.com/", "https://id.internal.example.net", "orders-api"]),
        "aud": rng.choice(["billing", "api://default", "reporting"]),
        "iat": iat,
        "exp": iat + rng.choice([900, 3600, 43200, 86400]),
        "scope": rng.choice(["read:orders", "admin", "openid profile", "repo:write"]),
    }
    siglen = {"HS256": 43, "RS256": 342, "ES256": 86}[alg]
    v = (b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
         + b64url(json.dumps(payload, separators=(",", ":")).encode()) + "." + rs(rng, siglen, B64URL))
    key = rng.choice(["ACCESS_TOKEN", "AUTH_TOKEN", "SESSION_JWT", "ID_TOKEN", "BEARER_TOKEN"])
    return [Item(v, "SECRET_JWT", "jwt_" + alg.lower(), key, "access token")]


PEM_KINDS = {
    # name: (header word, DER/base64 prefix, body length, line width)
    "rsa": ("RSA PRIVATE KEY", "MIIEowIBAAKCAQEA", 1588, 64),
    "pkcs8": ("PRIVATE KEY", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ", 1620, 64),
    "ec": ("EC PRIVATE KEY", "MHcCAQEEI", 164, 64),
    "openssh": ("OPENSSH PRIVATE KEY",
                "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQAAACA",
                396, 70),
}


def pem_private_key(rng, kind: str | None = None):
    kind = kind or rng.choice(sorted(PEM_KINDS))
    word, prefix, n, width = PEM_KINDS[kind]
    body = prefix + rs(rng, n - len(prefix), B64)
    if kind != "openssh" and rng.random() < 0.5:
        body = body[:-1] + "="
    lines = [body[i:i + width] for i in range(0, len(body), width)]
    v = f"-----BEGIN {word}-----\n" + "\n".join(lines) + f"\n-----END {word}-----"
    # Field names must fit the key type: an OpenSSH key is never a TLS key, and so on.
    names = {"openssh": ["SSH_PRIVATE_KEY", "DEPLOY_KEY"],
             "rsa": ["PRIVATE_KEY", "TLS_PRIVATE_KEY", "DEPLOY_KEY", "SIGNING_KEY", "SSH_PRIVATE_KEY"],
             "pkcs8": ["PRIVATE_KEY", "TLS_PRIVATE_KEY", "SIGNING_KEY"],
             "ec": ["PRIVATE_KEY", "TLS_PRIVATE_KEY", "SIGNING_KEY"]}[kind]
    key = rng.choice(names)
    return [Item(v, "SECRET_PRIVATE_KEY", "pem_" + kind, key, "private key")]


DB_USERS = ["app", "svc_orders", "admin", "reporting_ro", "etl", "billing_rw", "root", "analytics"]
DB_NAMES = ["orders", "ledger", "users", "inventory", "events", "crm", "payments", "audit"]
DB_HOST_SUFFIX = ["corp.internal", "svc.cluster.local", "db.example.net", "prod.example.org"]


def db_url(rng):
    scheme = rng.choice(["postgresql", "postgres", "mysql", "mongodb+srv", "redis", "amqp", "mssql"])
    pwd = rs(rng, rng.randint(14, 28), B62 + "-_.~")
    host = (f"{rng.choice(['pg', 'db', 'mysql', 'mongo', 'cache', 'mq', 'sql'])}-"
            f"{rng.choice(['prod', 'stage', 'eu1', 'core', 'east'])}-{rng.randint(1, 9)}."
            f"{rng.choice(DB_HOST_SUFFIX)}")
    user = rng.choice(DB_USERS)
    db = rng.choice(DB_NAMES)
    if scheme == "mongodb+srv":
        v = f"{scheme}://{user}:{pwd}@{host}/{db}?retryWrites=true&w=majority"
    elif scheme == "redis":
        v = f"redis://default:{pwd}@{host}:6379/{rng.randint(0, 5)}"
    elif scheme == "amqp":
        v = f"amqp://{user}:{pwd}@{host}:5672/{db}"
    else:
        port = {"postgresql": 5432, "postgres": 5432, "mysql": 3306, "mssql": 1433}[scheme]
        v = f"{scheme}://{user}:{pwd}@{host}:{port}/{db}"
        if rng.random() < 0.3 and scheme.startswith("postgres"):
            v += "?sslmode=require"
    # The field name must agree with the scheme (no MONGO_URI holding a mysql URL).
    if scheme == "mongodb+srv":
        names = ["DATABASE_URL", "MONGO_URI", "MONGODB_URL"]
    elif scheme == "redis":
        names = ["REDIS_URL", "CACHE_URL", "BROKER_URL"]
    elif scheme == "amqp":
        names = ["BROKER_URL", "AMQP_URL", "RABBITMQ_URL"]
    else:
        names = ["DATABASE_URL", "DB_URL", "SQLALCHEMY_DATABASE_URI"]
    key = rng.choice(names)
    return [Item(v, "SECRET_DB_URL", "db_url_" + scheme.replace("+", "_"), key, "database connection string")]


def slack_token(rng):
    pfx = rng.choice(["xoxb-", "xoxp-"])
    v = pfx + rs(rng, 12, string.digits) + "-" + rs(rng, 13, string.digits) + "-" + rs(rng, 24, B62)
    if pfx == "xoxb-":
        return [Item(v, "SECRET_GENERIC", "slack_token", "SLACK_BOT_TOKEN", "Slack bot token")]
    return [Item(v, "SECRET_GENERIC", "slack_token", "SLACK_USER_TOKEN", "Slack user token")]


def stripe_live(rng):
    pfx = rng.choice(["sk_live_", "rk_live_"])
    v = pfx + rs(rng, rng.choice([24, 99]), B62)
    if pfx == "sk_live_":
        return [Item(v, "SECRET_GENERIC", "stripe_live_key", "STRIPE_SECRET_KEY", "Stripe secret key")]
    return [Item(v, "SECRET_GENERIC", "stripe_live_key", "STRIPE_RESTRICTED_KEY", "Stripe restricted key")]


def sendgrid_key(rng):
    v = "SG." + rs(rng, 22, B64URL) + "." + rs(rng, 43, B64URL)
    return [Item(v, "SECRET_GENERIC", "sendgrid_key", "SENDGRID_API_KEY", "SendGrid API key")]


def twilio_key(rng):
    return [Item("SK" + rs(rng, 32, HEX), "SECRET_GENERIC", "twilio_api_key", "TWILIO_API_KEY", "Twilio API key")]


def npm_token(rng):
    return [Item("npm_" + rs(rng, 36, B62), "SECRET_GENERIC", "npm_token", "NPM_TOKEN", "npm publish token")]


def mailgun_key(rng):
    return [Item("key-" + rs(rng, 32, HEX), "SECRET_GENERIC", "mailgun_key", "MAILGUN_API_KEY", "Mailgun key")]


def shopify_token(rng):
    return [Item("shpat_" + rs(rng, 32, HEX), "SECRET_GENERIC", "shopify_token",
                 "SHOPIFY_ACCESS_TOKEN", "Shopify admin token")]


def datadog_key(rng):
    return [Item(rs(rng, 32, HEX), "SECRET_GENERIC", "datadog_api_key", "DD_API_KEY", "Datadog API key")]


def plain_password(rng):
    v = rs(rng, rng.randint(14, 24), B62 + "!#%^*-_+=")
    key = rng.choice(["DB_PASSWORD", "SMTP_PASSWORD", "ADMIN_PASSWORD", "REDIS_PASSWORD", "LDAP_BIND_PASSWORD"])
    return [Item(v, "SECRET_GENERIC", "password", key, "password")]


SECRET_FAMILIES: dict[str, list[Callable[[random.Random], list[Item]]]] = {
    "SECRET_CLOUD_KEY": [aws_pair, gcp_api_key, azure_storage_key, do_token, alibaba_key, cloudflare_token],
    "SECRET_AI_KEY": [openai_key, anthropic_key, hf_token, groq_key, replicate_token, perplexity_key, mistral_key],
    "SECRET_VCS_TOKEN": [github_classic, github_fine_grained, gitlab_pat, gitlab_trigger, bitbucket_app_password],
    "SECRET_JWT": [jwt],
    "SECRET_PRIVATE_KEY": [pem_private_key],
    "SECRET_DB_URL": [db_url],
    "SECRET_GENERIC": [slack_token, stripe_live, sendgrid_key, twilio_key, npm_token, mailgun_key,
                       shopify_token, datadog_key, plain_password],
}
SECRET_LABEL_ORDER = sorted(SECRET_FAMILIES)


# ---- PII. All synthetic: reserved phone ranges, documentation IP ranges,
# example and .test email domains, Luhn-valid random card numbers.

FIRST = ["Amara", "Bogdan", "Chiara", "Dmitri", "Eleni", "Farouk", "Greta", "Hiroshi", "Ingrid", "Jamal",
         "Keziah", "Lorenzo", "Maribel", "Nikhil", "Oksana", "Pradeep", "Quentin", "Rosalind", "Soren",
         "Tamsin", "Ulrich", "Valentina", "Wendell", "Xiomara", "Yusuf", "Zofia", "Anouk", "Bartek",
         "Cosima", "Desmond", "Esperanza", "Fintan", "Gulnara", "Hollis", "Imogen", "Joaquin"]
LAST = ["Okonkwo", "Lindqvist", "Marchetti", "Haddad", "Petrov", "Nakamura", "Abernathy", "Delacroix",
        "Szymanski", "Oyelaran", "Fairbanks", "Castellanos", "Ruisdael", "Thorvaldsen", "Mbeki", "Quispe",
        "Halloran", "Vasquez-Ortiz", "Kowalczyk", "Brannigan", "Achterberg", "Iwasaki", "Pemberton", "Zeller"]
STREETS = ["Elmwood Ave", "Larkspur Ln", "Quarry Rd", "Halsey St", "Old Mill Ct", "Birchfield Dr",
           "Canal St", "Juniper Way", "Ridgeview Blvd", "Tannery Row", "Marlowe Pl", "Saddleback Cir"]
CITIES = [("Springfield", "IL", "627"), ("Fairview", "OR", "970"), ("Greenville", "SC", "296"),
          ("Madison", "WI", "537"), ("Clinton", "IA", "527"), ("Salem", "MA", "019"),
          ("Georgetown", "TX", "786"), ("Franklin", "TN", "370"), ("Arlington", "VA", "222")]
# Only RFC 2606 domains that sit on real public suffixes. Presidio validates the
# TLD, and .test or .example domains would be rejected for an artifact reason.
EMAIL_DOMAINS = ["example.com", "example.org", "example.net"]
HEALTH = ["chemotherapy for stage II colon cancer", "treatment for bipolar disorder", "dialysis three times a week",
          "insulin therapy for type 1 diabetes", "a recent HIV diagnosis", "rehab for opioid use disorder",
          "physical therapy after a spinal fusion", "medication for severe depression",
          "follow-up care after a miscarriage", "an early-onset Parkinson's diagnosis",
          "weekly sessions for PTSD", "a pending kidney transplant"]
CONTEXTUAL = ["the only left-handed cellist in the Duluth symphony",
              "the night-shift charge nurse on ward 4B at St. Brigid's",
              "the CFO's younger brother who runs the Fresno warehouse",
              "the tallest player on the 2019 Lakeview East volleyball team",
              "the one engineer on the Tallinn team who uses a wheelchair",
              "the person who filed the harassment complaint against the store manager in March",
              "the only Farsi-speaking paralegal at the Boise office",
              "the employee whose badge was used at 3 a.m. on the night of the outage",
              "the oldest volunteer at the Harbor Street food bank",
              "the intern who was arrested at the October protest"]


def _luhn_complete(prefix: str, length: int, rng) -> str:
    body = prefix + rs(rng, length - len(prefix) - 1, string.digits)
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


def person_name(rng) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def pii_item(rng, label: str) -> Item:
    if label == "EMAIL":
        f, l = rng.choice(FIRST).lower(), rng.choice(LAST).lower().replace("-", "")
        local = rng.choice([f"{f}.{l}", f"{f[0]}{l}", f"{f}_{l}{rng.randint(1, 99)}", f"{l}.{f[0]}"])
        return Item(f"{local}@{rng.choice(EMAIL_DOMAINS)}", label, "email", "CONTACT_EMAIL", "email")
    if label == "PHONE":
        n = rng.randint(0, 99)
        v = rng.choice([
            f"({rng.choice(['415', '212', '312', '617', '503'])}) 555-01{n:02d}",
            f"+1 {rng.choice(['206', '404', '713'])}-555-01{n:02d}",
            f"+44 20 7946 0{rng.randint(0, 999):03d}",
            f"{rng.choice(['646', '702'])}.555.01{n:02d}",
        ])
        return Item(v, label, "phone", "PHONE_NUMBER", "phone number")
    if label == "CARD":
        kind = rng.choice(["visa", "mc", "amex"])
        if kind == "visa":
            num = _luhn_complete("4", 16, rng)
        elif kind == "mc":
            num = _luhn_complete("5" + str(rng.randint(1, 5)), 16, rng)
        else:
            num = _luhn_complete(rng.choice(["34", "37"]), 15, rng)
        style = rng.choice(["space", "dash", "plain"])
        if style != "plain":
            sep = " " if style == "space" else "-"
            groups = [num[0:4], num[4:10], num[10:]] if len(num) == 15 else [num[i:i + 4] for i in range(0, 16, 4)]
            num = sep.join(groups)
        return Item(num, label, "card_" + kind, "CARD_NUMBER", "card number")
    if label == "GOV_ID":
        if rng.random() < 0.75:
            area = rng.choice([a for a in range(1, 900) if a != 666])
            v = f"{area:03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"
            return Item(v, label, "us_ssn", "SSN", "social security number")
        v = (rng.choice(["AB", "CE", "JG", "NH", "PR", "SK"]) + " " + " ".join(rs(rng, 2, string.digits) for _ in range(3))
             + " " + rng.choice("ABCD"))
        return Item(v, label, "uk_nino", "NI_NUMBER", "national insurance number")
    if label == "IP_ADDRESS":
        r = rng.random()
        if r < 0.6:
            v = f"{rng.choice(['192.0.2', '198.51.100', '203.0.113'])}.{rng.randint(1, 254)}"
        elif r < 0.85:
            v = f"{rng.choice([23, 45, 67, 81, 104, 151, 172])}.{rng.randint(32, 250)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        else:
            v = f"2001:db8:{rs(rng, 4, HEX)}:{rs(rng, 4, HEX)}::{rs(rng, 3, HEX)}"
        return Item(v, label, "ip", "CLIENT_IP", "client IP")
    if label == "PERSON":
        return Item(person_name(rng), label, "person", "CUSTOMER_NAME", "customer name")
    if label == "ADDRESS":
        city, st, z = rng.choice(CITIES)
        v = f"{rng.randint(12, 9890)} {rng.choice(STREETS)}, {city}, {st} {z}{rng.randint(0, 99):02d}"
        return Item(v, label, "address", "SHIPPING_ADDRESS", "mailing address")
    if label == "HEALTH":
        return Item(rng.choice(HEALTH), label, "health", "MEDICAL_NOTE", "medical note")
    if label == "CONTEXTUAL":
        return Item(rng.choice(CONTEXTUAL), label, "contextual", "REPORTER_DESCRIPTION", "reporter")
    raise ValueError(label)


PATTERN_PII = ["EMAIL", "PHONE", "CARD", "GOV_ID", "IP_ADDRESS"]
CONTEXT_PII = ["PERSON", "ADDRESS", "HEALTH", "CONTEXTUAL"]


# ---- benign lookalikes (hard negatives). Opaque random values only go into
# clearly non-secret names. Placeholders, documented examples, masked values
# and public material go into secret-sounding names on purpose.


def _uuid(rng) -> str:
    return f"{rs(rng, 8, HEX)}-{rs(rng, 4, HEX)}-4{rs(rng, 3, HEX)}-{rng.choice('89ab')}{rs(rng, 3, HEX)}-{rs(rng, 12, HEX)}"


BENIGN_SENTENCES = ["page=3&size=50&sort=created_at", '{"offset":1200,"limit":100}', "last_seen=2026-10-02T11:04:00Z",
                    "cursor:orders:000918", '{"tab":"billing","collapsed":false}', "v=2;region=eu-west-1;shard=7"]

AWS_DOC_EXAMPLES = [("AKIAIOSFODNN7EXAMPLE", "AWS_ACCESS_KEY_ID"),
                    ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "AWS_SECRET_ACCESS_KEY")]

PLACEHOLDERS = [
    ("<YOUR_API_KEY>", "OPENAI_API_KEY"), ("your-api-key-here", "ANTHROPIC_API_KEY"),
    ("${OPENAI_API_KEY}", "OPENAI_API_KEY"), ("${{ secrets.GITHUB_TOKEN }}", "GITHUB_TOKEN"),
    ("REPLACE_ME", "STRIPE_SECRET_KEY"), ("ghp_" + "x" * 36, "GITHUB_TOKEN"), ("sk-...", "OPENAI_API_KEY"),
    ("<redacted>", "DB_PASSWORD"), ("********", "ADMIN_PASSWORD"), ("TODO: fill in", "SENDGRID_API_KEY"),
    ("postgresql://USER:PASSWORD@HOST:5432/DBNAME", "DATABASE_URL"), ("hf_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "HF_TOKEN"),
    ("xoxb-your-bot-token", "SLACK_BOT_TOKEN"), ("{{ vault_db_password }}", "DB_PASSWORD"),
    ("$(cat /run/secrets/api_key)", "API_KEY"), ("sk_live_...", "STRIPE_SECRET_KEY"),
    ("glpat-xxxxxxxxxxxxxxxxxxxx", "GITLAB_TOKEN"), ("<paste token here>", "NPM_TOKEN"),
]


def lookalike(rng, kind: str | None = None) -> Item:
    kinds = ["uuid", "git_sha", "sha256", "md5", "docker_digest", "b64_cursor", "integrity", "aws_example",
             "placeholder", "masked", "ssh_public", "public_pem", "semver", "db_url_nopass", "tracking_number",
             "invoice_id", "s3_path"]
    kind = kind or rng.choice(kinds)
    if kind == "uuid":
        return Item(_uuid(rng), None, kind, rng.choice(["REQUEST_ID", "TRACE_ID", "TENANT_ID", "ORDER_ID", "CORRELATION_ID"]), "request id")
    if kind == "git_sha":
        return Item(rs(rng, 40, HEX), None, kind, rng.choice(["COMMIT_SHA", "GIT_REVISION", "BUILD_COMMIT"]), "commit")
    if kind == "sha256":
        return Item(rs(rng, 64, HEX), None, kind, rng.choice(["ARTIFACT_SHA256", "CHECKSUM", "CONTENT_HASH"]), "checksum")
    if kind == "md5":
        return Item(rs(rng, 32, HEX), None, kind, rng.choice(["ETAG", "FILE_MD5"]), "etag")
    if kind == "docker_digest":
        return Item("sha256:" + rs(rng, 64, HEX), None, kind, "IMAGE_DIGEST", "image digest")
    if kind == "b64_cursor":
        raw = rng.choice(BENIGN_SENTENCES).encode()
        return Item(base64.b64encode(raw).decode(), None, kind, rng.choice(["NEXT_CURSOR", "PAGE_STATE"]), "pagination cursor")
    if kind == "integrity":
        return Item("sha512-" + rs(rng, 86, B64) + "==", None, kind, "INTEGRITY", "package integrity hash")
    if kind == "aws_example":
        v, k = rng.choice(AWS_DOC_EXAMPLES)
        return Item(v, None, kind, k, "example AWS key from the docs")
    if kind == "placeholder":
        v, k = rng.choice(PLACEHOLDERS)
        return Item(v, None, kind, k, "placeholder")
    if kind == "masked":
        v, k = rng.choice([
            ("sk-proj-****" + rs(rng, 4, B62), "OPENAI_API_KEY"), ("ghp_" + "*" * 32 + rs(rng, 4, B62), "GITHUB_TOKEN"),
            ("AKIA" + "*" * 12 + rs(rng, 4, B32), "AWS_ACCESS_KEY_ID"), ("****" + rs(rng, 4, string.digits), "CARD_LAST4"),
            ("postgres://app:****@db.internal:5432/app", "DATABASE_URL"),
        ])
        return Item(v, None, kind, k, "masked value")
    if kind == "ssh_public":
        v = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + rs(rng, 43, B64) + f" {rng.choice(['deploy', 'ci', 'ops'])}@{rng.choice(['build-01', 'laptop', 'bastion'])}"
        return Item(v, None, kind, rng.choice(["SSH_PUBLIC_KEY", "AUTHORIZED_KEY"]), "public key")
    if kind == "public_pem":
        word = rng.choice(["PUBLIC KEY", "CERTIFICATE"])
        body = ("MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA" if word == "PUBLIC KEY" else "MIIDdzCCAl+gAwIBAgIE")
        body += rs(rng, (392 if word == "PUBLIC KEY" else 1180) - len(body), B64)
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        v = f"-----BEGIN {word}-----\n" + "\n".join(lines) + f"\n-----END {word}-----"
        return Item(v, None, kind, "PUBLIC_KEY" if word == "PUBLIC KEY" else "CA_CERT", "public certificate")
    if kind == "semver":
        return Item(f"{rng.randint(0, 9)}.{rng.randint(0, 30)}.{rng.randint(0, 99)}+build.{rng.randint(1000, 99999)}",
                    None, kind, "APP_VERSION", "version")
    if kind == "db_url_nopass":
        v = rng.choice([f"postgresql://localhost:5432/{rng.choice(DB_NAMES)}_test", f"redis://cache:6379/{rng.randint(0, 3)}",
                        f"sqlite:///./{rng.choice(DB_NAMES)}.db", "mongodb://127.0.0.1:27017/dev"])
        return Item(v, None, kind, "DATABASE_URL", "local database url")
    if kind == "tracking_number":
        valid = _luhn_complete(rs(rng, 15, string.digits), 16, rng)
        # Swap the check digit for a wrong one so it is not a card number.
        num = valid[:-1] + str((int(valid[-1]) + rng.randint(1, 9)) % 10)
        assert _luhn_ok(valid) and not _luhn_ok(num)
        return Item(num, None, kind, "TRACKING_NUMBER", "tracking number")
    if kind == "invoice_id":
        return Item(f"INV-{rng.randint(2019, 2026)}-{rng.randint(0, 999999):06d}", None, kind, "INVOICE_ID", "invoice")
    if kind == "s3_path":
        return Item(f"s3://{rng.choice(['assets', 'logs', 'backups'])}-{rs(rng, 6, LOWER_DIGITS)}/{rng.choice(DB_NAMES)}/{rng.randint(2020, 2026)}/",
                    None, kind, "EXPORT_BUCKET_PATH", "bucket path")
    raise ValueError(kind)


def _luhn_ok(num: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


BENIGN_CONFIG = [("LOG_LEVEL", "info"), ("PORT", "8080"), ("REGION", "eu-west-1"), ("TIMEOUT_SECONDS", "30"),
                 ("FEATURE_FLAGS", "search,beta_ui"), ("MAX_RETRIES", "5"), ("SERVICE_NAME", "ledger-sync"),
                 ("API_BASE_URL", "https://api.payments.example.com/v2"), ("WORKERS", "4"), ("ENABLE_TRACING", "true"),
                 ("CACHE_TTL", "300"), ("NODE_ENV", "production"), ("TZ", "UTC"), ("QUEUE_NAME", "invoices-high"),
                 ("BATCH_SIZE", "250"), ("ALLOWED_ORIGINS", "https://app.example.com")]


def benign_config(rng) -> Item:
    k, v = rng.choice(BENIGN_CONFIG)
    return Item(v, None, "config", k, k.lower().replace("_", " "))


# --------------------------------------------------------------------------
# Doc builder: tracks char spans while text is assembled
# --------------------------------------------------------------------------


class Doc:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.n = 0
        self.spans: list[tuple[int, int, str]] = []
        self.check: list[tuple[int, int, str]] = []  # (start, end, expected segment) for self-check
        self.values: list[str] = []  # raw sensitive values, for cross-set disjointness

    def t(self, s: str) -> "Doc":
        self.parts.append(s)
        self.n += len(s)
        return self

    def v(self, item: Item, mode: str = "raw") -> "Doc":
        seg = encode(item.value, mode)
        start = self.n
        self.t(seg)
        if item.label is not None:
            self.spans.append((start, self.n, item.label))
            self.check.append((start, self.n, seg))
            self.values.append(item.value)
        return self

    def text(self) -> str:
        return "".join(self.parts)

    def splice(self, other: "Doc") -> None:
        off = self.n
        self.t(other.text())
        self.spans += [(a + off, b + off, lab) for a, b, lab in other.spans]
        self.check += [(a + off, b + off, s) for a, b, s in other.check]
        self.values += other.values


def encode(value: str, mode: str) -> str:
    if mode == "raw":
        return value
    if mode == "json":
        return json.dumps(value, ensure_ascii=False)[1:-1]
    if mode == "json2":  # a JSON string holding JSON: escape twice
        return json.dumps(json.dumps(value, ensure_ascii=False)[1:-1], ensure_ascii=False)[1:-1]
    if mode == "oneline":
        return value.replace("\n", "\\n")
    if mode == "py_in_json":  # python string literal inside a JSON notebook cell
        return json.dumps(value.replace("\n", "\\n"), ensure_ascii=False)[1:-1]
    if mode.startswith("indent"):
        return value.replace("\n", "\n" + " " * int(mode[6:]))
    if mode == "sql":
        return value.replace("'", "''")
    raise ValueError(mode)


def camel(key: str) -> str:
    a, *rest = key.lower().split("_")
    return a + "".join(w.capitalize() for w in rest)


def snake(key: str) -> str:
    return key.lower()


# --------------------------------------------------------------------------
# Carriers. Each takes (rng, items) and returns a Doc. Items mix sensitive
# values and benign filler, already shuffled by the caller.
# --------------------------------------------------------------------------

TOOLS = ["deploy_service", "rotate_credentials", "create_webhook", "sync_inventory", "send_invoice",
         "provision_bucket", "update_secret", "run_migration", "register_integration", "query_customers"]


def c_mcp_tools_call(rng, items):
    d = Doc()
    style = rng.choice([snake, camel])
    d.t('{\n  "jsonrpc": "2.0",\n  "id": ' + str(rng.randint(1, 999)) + ',\n  "method": "tools/call",\n'
        '  "params": {\n    "name": "' + rng.choice(TOOLS) + '",\n    "arguments": {\n')
    for i, it in enumerate(items):
        d.t(f'      "{style(it.key)}": "').v(it, "json").t('"' + ("," if i < len(items) - 1 else "") + "\n")
    d.t("    }\n  }\n}")
    return d


def c_agent_react(rng, items):
    d = Doc()
    tool = rng.choice(TOOLS)
    d.t(f"Thought: The user wants me to {tool.replace('_', ' ')}. I have the values I need from the earlier message.\n")
    d.t(f"Action: {tool}\nAction Input: {{")
    for i, it in enumerate(items):
        d.t(("" if i == 0 else ", ") + f'"{snake(it.key)}": "').v(it, "json").t('"')
    d.t("}\n")
    d.t(rng.choice(["Observation: {\"status\": \"ok\", \"elapsed_ms\": " + str(rng.randint(40, 900)) + "}\n",
                    "Observation: request accepted, job queued\n", "Observation: 202 Accepted\n"]))
    d.t("Thought: " + rng.choice(["Done. I'll confirm with the user.", "That worked, moving to the next step.",
                                  "Now I need to verify the change took effect."]))
    return d


def c_python_module(rng, items):
    d = Doc()
    d.t(rng.choice(['"""Runtime settings for the worker."""\n', "# settings.py\n", "# auto-generated, do not edit\n"]))
    d.t("import os\n\n")
    for it in items:
        if it.multiline:
            d.t(f'{it.key} = """').v(it).t('"""\n')
        else:
            q = rng.choice(['"', "'"])
            d.t(f"{it.key} = {q}").v(it).t(f"{q}\n")
    d.t(rng.choice(["\nDEBUG = False\n", "\nTIMEOUT = 30\n", "\n__all__ = []\n"]))
    return d


def c_node_config(rng, items):
    d = Doc()
    d.t(rng.choice(["module.exports = {\n", "export default {\n", "const config = {\n"]))
    for it in items:
        if it.multiline:
            d.t(f"  {camel(it.key)}: `").v(it).t("`,\n")
        else:
            d.t(f"  {camel(it.key)}: '").v(it).t("',\n")
    d.t("};\n")
    return d


def c_compose_yaml(rng, items):
    d = Doc()
    svc = rng.choice(["api", "worker", "scheduler", "gateway"])
    d.t(f"services:\n  {svc}:\n    image: registry.example.com/{svc}:{rng.randint(1, 40)}.{rng.randint(0, 9)}\n"
        "    restart: unless-stopped\n    environment:\n")
    for it in items:
        d.t(f'      {it.key}: "').v(it, "json").t('"\n')
    d.t("    ports:\n      - \"8080:8080\"\n")
    return d


def c_k8s_manifest(rng, items):
    d = Doc()
    d.t(f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {rng.choice(['app-credentials', 'integrations', 'runtime-env'])}\n"
        f"  namespace: {rng.choice(['prod', 'payments', 'default', 'staging'])}\ntype: Opaque\nstringData:\n")
    for it in items:
        if it.multiline:
            d.t(f"  {snake(it.key)}: |\n    ").v(it, "indent4").t("\n")
        else:
            d.t(f"  {snake(it.key)}: ").v(it).t("\n")
    return d


def c_terraform(rng, items):
    d = Doc()
    d.t(rng.choice(["# terraform.tfvars\n", "# managed by platform team\n", ""]))
    for it in items:
        if it.multiline:
            d.t(f"{snake(it.key)} = <<EOT\n").v(it).t("\nEOT\n")
        else:
            d.t(f'{snake(it.key)} = "').v(it).t('"\n')
    return d


def c_git_diff(rng, items):
    d = Doc()
    path = rng.choice(["config/.env.production", "deploy/values.env", "scripts/bootstrap.env", ".env"])
    d.t(f"diff --git a/{path} b/{path}\nindex {rs(rng, 7, HEX)}..{rs(rng, 7, HEX)} 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{rng.randint(1, 40)},3 +{rng.randint(1, 40)},{len(items) + 3} @@\n")
    d.t(" # runtime configuration\n")
    for it in items:
        d.t(f"+{it.key}=").v(it, "oneline").t("\n")
    d.t(" LOG_FORMAT=json\n")
    return d


def c_systemd_unit(rng, items):
    d = Doc()
    name = rng.choice(["ledger-sync", "mailer", "edge-proxy", "report-builder"])
    d.t(f"[Unit]\nDescription={name} service\nAfter=network-online.target\n\n[Service]\nUser={name}\n")
    for it in items:
        d.t(f'Environment="{it.key}=').v(it, "oneline").t('"\n')
    d.t(f"ExecStart=/opt/{name}/bin/{name} --serve\nRestart=on-failure\n\n[Install]\nWantedBy=multi-user.target\n")
    return d


def c_ini_profile(rng, items):
    d = Doc()
    d.t(f"[{rng.choice(['default', 'prod', 'deploy', 'ci-runner'])}]\n")
    for it in items:
        d.t(f"{snake(it.key)} = ").v(it, "oneline").t("\n")
    return d


def c_notebook_json(rng, items):
    d = Doc()
    d.t('{\n "cells": [\n  {\n   "cell_type": "code",\n   "execution_count": ' + str(rng.randint(1, 30)) +
        ',\n   "metadata": {},\n   "outputs": [],\n   "source": [\n    "import os\\n",\n')
    for it in items:
        d.t(f'    "os.environ[\\"{it.key}\\"] = \\"').v(it, "py_in_json").t('\\"\\n",\n')
    d.t('    "client = make_client()"\n   ]\n  }\n ],\n "nbformat": 4,\n "nbformat_minor": 5\n}')
    return d


def c_sql_insert(rng, items):
    d = Doc()
    table = rng.choice(["integrations", "service_accounts", "customer_records", "vendor_settings"])
    d.t(f"-- migration {rng.randint(100, 999)}: seed {table}\n")
    d.t(f"INSERT INTO {table} (" + ", ".join(snake(it.key) for it in items) + ")\nVALUES (")
    for i, it in enumerate(items):
        d.t(("" if i == 0 else ", ") + "'").v(it, "sql").t("'")
    d.t(");\n")
    return d


def c_jsonl_log(rng, items):
    d = Doc()
    for i, it in enumerate(items):
        ts = f"2026-{rng.randint(1, 10):02d}-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}.{rng.randint(0, 999):03d}Z"
        lvl = rng.choice(["DEBUG", "INFO", "WARN"])
        msg = rng.choice(["outbound request", "loaded config", "retrying call", "handler input", "cache miss"])
        d.t(f'{{"ts":"{ts}","level":"{lvl}","logger":"{rng.choice(["http", "cfg", "worker", "auth"])}","msg":"{msg}",'
            f'"{snake(it.key)}":"').v(it, "json").t('"}\n')
    return d


def c_curl_command(rng, items):
    d = Doc()
    host = rng.choice(["api.vendor.example.com", "hooks.example.net", "internal-gw.example.org"])
    d.t(f"curl -sS -X POST https://{host}/v{rng.randint(1, 3)}/{rng.choice(['events', 'jobs', 'records'])} \\\n")
    d.t('  -H "Content-Type: application/json" \\\n')
    body = []
    for i, it in enumerate(items):
        if i == 0 and not it.multiline:
            hdr = rng.choice(["Authorization: Bearer ", "X-Api-Key: ", f"X-{it.key.title().replace('_', '-')}: "])
            d.t(f'  -H "{hdr}').v(it).t('" \\\n')
        else:
            body.append(it)
    d.t("  -d '{")
    for i, it in enumerate(body):
        d.t(("" if i == 0 else ",") + f'"{snake(it.key)}":"').v(it, "json").t('"')
    d.t("}'")
    return d


def c_markdown_howto(rng, items):
    d = Doc()
    d.t(rng.choice(["## Local setup\n\n", "### Running the integration tests\n\n", "## Quickstart\n\n"]))
    d.t("Export these before you start the dev server:\n\n```bash\n")
    for it in items:
        d.t(f'export {it.key}="').v(it, "oneline").t('"\n')
    d.t("```\n\nThen run `make dev`.\n")
    return d


def c_crm_tool_result(rng, items):
    d = Doc()
    d.t('{"jsonrpc":"2.0","id":' + str(rng.randint(1, 999)) + ',"result":{"content":[{"type":"text","text":"{')
    for i, it in enumerate(items):
        d.t(("" if i == 0 else ",") + '\\"' + snake(it.key) + '\\":\\"').v(it, "json2").t('\\"')
    d.t('}"}],"isError":false}}')
    return d


def c_csv_export(rng, items):
    d = Doc()
    d.t(",".join(snake(it.key) for it in items) + "\n")
    for i, it in enumerate(items):
        q = "," in it.value or "\n" in it.value
        d.t(("" if i == 0 else ",") + ('"' if q else "")).v(it, "raw").t('"' if q else "")
    d.t("\n")
    return d


# Prose carriers use label-aware sentence templates.

PROSE = {
    "SECRET": ["here is the {desc}: {v}", "use {v} as the {desc} for now", "{desc} = {v}",
               "the {desc} we got from the vendor portal is {v}", "I set the {desc} to {v} but it still returns 401",
               "can you check whether {v} is still a valid {desc}?"],
    "SECRET_ML": ["here is the {desc}:\n\n{v}\n", "the {desc} is below, please install it on the box\n{v}\n"],
    "EMAIL": ["you can reach me at {v}", "her email is {v}", "cc {v} on the reply", "the invoice went to {v}"],
    "PHONE": ["call back on {v}", "phone: {v}", "my cell is {v}", "he left {v} as a contact number"],
    "CARD": ["charge it to {v}, exp 09/29", "card number {v}", "the refund should go back to {v}"],
    "GOV_ID": ["my social is {v}", "SSN on file: {v}", "for verification the ID number is {v}"],
    "IP_ADDRESS": ["the requests came from {v}", "please allowlist {v}", "her laptop was on {v} at the time"],
    "PERSON": ["{v} from accounting asked about this", "I spoke with {v} today", "assigning this to {v}"],
    "ADDRESS": ["ship it to {v}", "mailing address is {v}", "they moved to {v} last spring"],
    "HEALTH": ["she is on leave for {v}", "medical note: receiving {v}", "he mentioned he is going through {v}"],
    "CONTEXTUAL": ["the complaint was filed by {v}", "this came from {v}", "it was {v} who reported it"],
    None: ["request id {v}", "reference {v}", "{desc}: {v}", "for the record the {desc} is {v}"],
}


def _sentence(rng, d: Doc, it: Item) -> None:
    if it.family == "config":
        tpl = "{desc} is set to {v}"
    elif it.label is None:
        tpl = rng.choice(PROSE[None])
    elif it.label.startswith("SECRET"):
        tpl = rng.choice(PROSE["SECRET_ML" if it.multiline else "SECRET"])
    else:
        tpl = rng.choice(PROSE[it.label])
    pre, post = tpl.split("{v}")
    d.t(pre.format(desc=it.desc)).v(it).t(post.format(desc=it.desc))


def c_chat_paste(rng, items):
    d = Doc()
    d.t(rng.choice(["hey, quick one. ", "Hi! ", "ok so ", "Morning. ", ""]))
    for i, it in enumerate(items):
        _sentence(rng, d, it)
        d.t(rng.choice([". ", ". ", "\n", "; "]) if i < len(items) - 1 else ".")
    d.t(rng.choice([" thanks!", " any idea what's wrong?", " let me know if you need anything else.", ""]))
    return d


def c_email_body(rng, items):
    d = Doc()
    d.t(f"Subject: {rng.choice(['Re: onboarding', 'access for the new vendor', 'Fwd: ticket follow-up', 'handover notes'])}\n\n")
    d.t(rng.choice(["Hi team,\n\n", "Hello,\n\n", "Hi all,\n\n"]))
    for it in items:
        _sentence(rng, d, it)
        d.t(".\n")
    d.t("\nBest,\n" + rng.choice(FIRST) + "\n")
    return d


def c_support_ticket(rng, items):
    d = Doc()
    d.t(f"Ticket #{rng.randint(10000, 99999)}\nPriority: {rng.choice(['P2', 'P3', 'P1'])}\nStatus: open\n\nDescription:\n")
    for it in items:
        _sentence(rng, d, it)
        d.t(". ")
    d.t("\n\nInternal notes: escalate if not resolved by Friday.\n")
    return d


CARRIERS: dict[str, Callable] = {
    "mcp_tools_call": c_mcp_tools_call, "agent_react": c_agent_react, "python_module": c_python_module,
    "node_config": c_node_config, "compose_yaml": c_compose_yaml, "k8s_manifest": c_k8s_manifest,
    "terraform": c_terraform, "git_diff": c_git_diff, "systemd_unit": c_systemd_unit, "ini_profile": c_ini_profile,
    "notebook_json": c_notebook_json, "sql_insert": c_sql_insert, "jsonl_log": c_jsonl_log,
    "curl_command": c_curl_command, "markdown_howto": c_markdown_howto, "crm_tool_result": c_crm_tool_result,
    "csv_export": c_csv_export, "chat_paste": c_chat_paste, "email_body": c_email_body, "support_ticket": c_support_ticket,
}
CARRIER_ORDER = list(CARRIERS)
PII_CARRIERS = ["crm_tool_result", "csv_export", "chat_paste", "email_body", "support_ticket", "jsonl_log",
                "sql_insert", "mcp_tools_call"]


# --------------------------------------------------------------------------
# Set builders
# --------------------------------------------------------------------------


class Registry:
    """Keeps sensitive values unique within and across sets."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def fresh(self, make: Callable[[], list[Item]]) -> list[Item]:
        for _ in range(100):
            items = make()
            vals = [it.value for it in items if it.label is not None]
            if not any(v in self.seen for v in vals):
                self.seen.update(vals)
                return items
        raise RuntimeError("could not draw a unique value")


def draw_secret(rng, reg: Registry, label: str | None = None) -> list[Item]:
    label = label or rng.choice(SECRET_LABEL_ORDER)
    fam = rng.choice(SECRET_FAMILIES[label])
    return reg.fresh(lambda: fam(rng))


def draw_pii(rng, reg: Registry, label: str) -> list[Item]:
    # Contextual and health phrases come from small pools, so they may repeat.
    if label in ("HEALTH", "CONTEXTUAL"):
        return [pii_item(rng, label)]
    return reg.fresh(lambda: [pii_item(rng, label)])


def make_example(ex_id: str, d: Doc, carrier: str, meta: dict, source: str = SOURCE, rung: int | None = None) -> Example:
    text = d.text()
    for a, b, seg in d.check:
        assert text[a:b] == seg, f"span self-check failed in {ex_id}"
    spans = sorted(d.spans)
    for (a1, b1, _), (a2, b2, _) in zip(spans, spans[1:]):
        assert b1 <= a2, f"overlapping spans in {ex_id}"
    ex = Example(id=ex_id, text=text, spans=[Span(a, b, lab) for a, b, lab in spans], carrier=carrier,
                 source=source, rung=rung, meta=meta)
    ex._values = d.values  # type: ignore[attr-defined]  # not serialized; used for disjointness checks
    return ex


def build_doc(rng, carrier: str, sensitive: list[Item], n_filler: int, filler_kinds: str = "mixed") -> Doc:
    fillers: list[Item] = []
    for _ in range(n_filler):
        if filler_kinds == "lookalike" or (filler_kinds == "mixed" and rng.random() < 0.5):
            fillers.append(lookalike(rng))
        else:
            fillers.append(benign_config(rng))
    items = sensitive + fillers
    rng.shuffle(items)
    # Avoid duplicate keys inside one doc (would look odd in structured carriers).
    seen: dict[str, int] = {}
    for it in items:
        if it.key in seen:
            seen[it.key] += 1
            it.key = f"{it.key}_{seen[it.key]}"
        else:
            seen[it.key] = 1
    return CARRIERS[carrier](rng, items)


def secret_positive(rng, reg, ex_id, carrier=None, pii_chance=0.25) -> Example:
    carrier = carrier or rng.choice(CARRIER_ORDER)
    sens = draw_secret(rng, reg)
    if rng.random() < 0.3:
        sens += draw_secret(rng, reg)
    if rng.random() < pii_chance:
        sens += draw_pii(rng, reg, rng.choice(PATTERN_PII + CONTEXT_PII))
    d = build_doc(rng, carrier, sens, rng.randint(1, 4))
    meta = {"kind": "positive", "families": sorted({it.family for it in sens if it.label})}
    return make_example(ex_id, d, carrier, meta)


def pii_positive(rng, reg, ex_id) -> Example:
    carrier = rng.choice(PII_CARRIERS)
    sens: list[Item] = []
    for _ in range(rng.randint(2, 4)):
        lab = rng.choice(PATTERN_PII) if rng.random() < 0.65 else rng.choice(CONTEXT_PII)
        sens += draw_pii(rng, reg, lab)
    d = build_doc(rng, carrier, sens, rng.randint(0, 2))
    meta = {"kind": "positive_pii", "families": sorted({it.family for it in sens})}
    return make_example(ex_id, d, carrier, meta)


def negative(rng, ex_id, carrier=None, filler_kinds="mixed", lo=2, hi=6, kind="negative") -> Example:
    carrier = carrier or rng.choice(CARRIER_ORDER)
    d = build_doc(rng, carrier, [], rng.randint(lo, hi), filler_kinds)
    return make_example(ex_id, d, carrier, {"kind": kind})


def build_secrets_eval(seed: int, reg: Registry) -> list[Example]:
    rng = random.Random(seed)
    out = []
    for i in range(1000):
        out.append(secret_positive(rng, reg, f"se-{i:05d}", carrier=CARRIER_ORDER[i % len(CARRIER_ORDER)]))
    for i in range(400):
        out.append(negative(rng, f"se-neg-{i:05d}", carrier=CARRIER_ORDER[i % len(CARRIER_ORDER)]))
    rng.shuffle(out)
    return out


def build_mixed(seed: int, reg: Registry, prefix: str) -> list[Example]:
    """dev and final share this recipe with different seeds."""
    rng = random.Random(seed)
    out = []
    for i in range(450):
        out.append(secret_positive(rng, reg, f"{prefix}-s-{i:05d}"))
    for i in range(300):
        out.append(pii_positive(rng, reg, f"{prefix}-p-{i:05d}"))
    for i in range(300):
        out.append(negative(rng, f"{prefix}-n-{i:05d}"))
    rng.shuffle(out)
    return out


def build_hardneg(seed: int) -> list[Example]:
    rng = random.Random(seed)
    out = []
    for i in range(800):
        out.append(negative(rng, f"hn-{i:05d}", carrier=CARRIER_ORDER[i % len(CARRIER_ORDER)],
                            filler_kinds="lookalike", lo=1, hi=5, kind="hardneg"))
    return out


# ---- long context: procedural benign filler with secrets at random depths

WORDS_N = ["pipeline", "scheduler", "invoice", "ledger", "cluster", "migration", "dashboard", "replica", "tenant",
           "rollout", "budget", "forecast", "warehouse", "shipment", "contract", "sprint", "incident", "retention",
           "partition", "index", "queue", "cohort", "vendor", "quota", "workflow", "template", "snapshot"]
WORDS_V = ["reviewed", "migrated", "postponed", "rebalanced", "summarized", "approved", "flagged", "archived",
           "deployed", "measured", "reduced", "extended", "documented", "rescheduled", "validated", "merged"]
WORDS_A = ["quarterly", "regional", "stale", "noisy", "critical", "legacy", "upstream", "nightly", "redundant",
           "experimental", "primary", "overdue", "partial", "cold", "shared"]


def _prose_para(rng) -> str:
    sents = []
    for _ in range(rng.randint(4, 9)):
        sents.append(f"The {rng.choice(WORDS_A)} {rng.choice(WORDS_N)} was {rng.choice(WORDS_V)} after the "
                     f"{rng.choice(WORDS_N)} {rng.choice(['review', 'sync', 'meeting', 'audit', 'retro'])}"
                     f"{rng.choice(['', ', and the team agreed to revisit it next week', ' because the numbers looked off', ' without any issues'])}.")
    return " ".join(sents) + "\n\n"


def _code_block(rng) -> str:
    fn = f"{rng.choice(WORDS_V)[:-2]}_{rng.choice(WORDS_N)}"
    arg = rng.choice(WORDS_N)
    lines = [f"def {fn}({arg}, limit={rng.randint(5, 500)}):",
             f'    """{rng.choice(WORDS_A).capitalize()} helper for {rng.choice(WORDS_N)} handling."""',
             "    results = []"]
    for _ in range(rng.randint(3, 8)):
        lines.append(f"    if {arg}.{rng.choice(WORDS_N)} > {rng.randint(0, 99)}:")
        lines.append(f"        results.append({arg}.{rng.choice(WORDS_N)}_{rng.choice(['id', 'count', 'total'])})")
    lines.append("    return results[:limit]")
    return "\n".join(lines) + "\n\n"


def _log_block(rng) -> str:
    out = []
    for _ in range(rng.randint(8, 25)):
        out.append(f"2026-{rng.randint(1, 10):02d}-{rng.randint(1, 28):02d} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:"
                   f"{rng.randint(0, 59):02d} {rng.choice(['INFO', 'DEBUG', 'WARN'])} [{rng.choice(WORDS_N)}] "
                   f"{rng.choice(WORDS_V)} {rng.choice(WORDS_N)} req={_uuid(rng)} took {rng.randint(1, 2000)}ms")
    return "\n".join(out) + "\n\n"


def _table_block(rng) -> str:
    rows = ["| item | owner | status | sha |", "| --- | --- | --- | --- |"]
    for _ in range(rng.randint(4, 12)):
        rows.append(f"| {rng.choice(WORDS_N)} | team-{rng.choice(WORDS_N)} | {rng.choice(WORDS_V)} | {rs(rng, 12, HEX)} |")
    return "\n".join(rows) + "\n\n"


def _yaml_block(rng) -> str:
    out = [f"{rng.choice(WORDS_N)}:"]
    for _ in range(rng.randint(4, 12)):
        it = rng.choice([benign_config(rng), lookalike(rng, rng.choice(["uuid", "git_sha", "semver", "sha256"]))])
        out.append(f"  {snake(it.key)}: {it.value}")
    return "\n".join(out) + "\n\n"


FILLERS = [_prose_para, _prose_para, _code_block, _log_block, _table_block, _yaml_block]
CHARS_PER_TOKEN = 2.0  # low end of Qwen3 tokenizer chars per token on this filler; measured counts in README.md
LONGCTX_CARRIERS = ["python_module", "jsonl_log", "chat_paste", "markdown_howto", "compose_yaml", "curl_command",
                    "mcp_tools_call", "git_diff", "ini_profile", "email_body"]


def build_longctx(seed: int, reg: Registry) -> list[Example]:
    rng = random.Random(seed)
    out = []
    plan = [True] * 90 + [False] * 60
    for i, positive in enumerate(plan):
        target_tokens = rng.randint(12000, 30000)
        target_chars = int(target_tokens * CHARS_PER_TOKEN)
        blocks: list[str] = []
        total = 0
        while total < target_chars:
            b = rng.choice(FILLERS)(rng)
            blocks.append(b)
            total += len(b)
        d = Doc()
        n_secret = rng.randint(1, 3) if positive else 0
        insert_at = sorted(rng.sample(range(len(blocks)), n_secret))
        depths = []
        for j, b in enumerate(blocks):
            if j in insert_at:
                sub = build_doc(rng, rng.choice(LONGCTX_CARRIERS), draw_secret(rng, reg), rng.randint(0, 2))
                depths.append(round(d.n / max(total, 1), 3))
                d.splice(sub)
                d.t("\n\n")
            d.t(b)
        meta = {"kind": "positive" if positive else "negative", "target_tokens": target_tokens,
                "depths": depths, "n_secrets": n_secret}
        out.append(make_example(f"lc-{i:04d}", d, "long_mixed", meta))
    rng.shuffle(out)
    return out


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    reg = Registry()  # one registry across the value-bearing sets keeps them disjoint
    sets = {
        "secrets_eval": build_secrets_eval(1101, reg),
        "dev": build_mixed(2202, reg, "dev"),
        "final": build_mixed(3303, reg, "final"),
        "hardneg": build_hardneg(4404),
        "longctx": build_longctx(5505, reg),
    }
    for name, exs in sets.items():
        n_pos = sum(1 for e in exs if e.spans)
        n_spans = sum(len(e.spans) for e in exs)
        write_jsonl(out / f"{name}.jsonl", exs)
        print(f"{name}: {len(exs)} docs, {n_pos} with spans, {n_spans} spans, {len(exs) - n_pos} without")
    assert not any(e.spans for e in sets["hardneg"])


if __name__ == "__main__":
    main()
