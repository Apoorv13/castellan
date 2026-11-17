"""Frozen obfuscation ladder generator (CLAUDE.md section 6).

Run from the repo root:

    python eval_data/ladder.py --out eval_data

Each base secret is rendered once per rung, 0 to 5:

    0  plain, in a realistic carrier
    1  split across two variables or fields
    2  base64 or hex encoded
    3  homoglyphs and/or zero-width characters inside the secret
    4  spelled out in words
    5  pieces spread across chat turns, with an instruction to join them

Rungs 0 to 4 keep the base's carrier. Rung 5 is always a Qwen3 ChatML
conversation that ends on a user turn. Every piece of secret material gets
its own span with the base's label. Two kinds of negatives carry no spans:

- hardneg: 180 lookalike bases (placeholders, example keys, IDs) at rungs 0 to 5.
- control: one matched benign base per secret base (commit SHAs, request IDs,
  cursors, digests, short EC public keys for the PEM bases) at rungs 2 to 5,
  with the same templates, carriers and announced split as the secrets.

Rung 2 to 5 items carry ``meta.announced``. Rung 2 is always announced (note
plus _BASE64/_HEX suffix), rung 3 never is, and rungs 4 and 5 are announced for
exactly half of the bases: the hint comment, the _SPELLED suffix and the "chat
filter blocks it" line appear only on announced items. Rung 5 always keeps
the join instruction.

``meta.target_sha256`` is the sha256 of the plain base value with all
whitespace removed. readability.py can score an exact match against it
without the value ever being stored or logged a second time.

The ladder is a measurement. Never tune anything against it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secrets_eval import (  # noqa: E402
    B64,
    SECRET_FAMILIES,
    SECRET_LABEL_ORDER,
    TOOLS,
    Doc,
    Item,
    Registry,
    lookalike,
    make_example,
    pem_private_key,
    write_jsonl,
)

SOURCE = "eval:ladder"
CARRIERS = ["env_file", "python_source", "json_tool_args", "yaml_config", "prose_message"]
MULTILINE_CARRIERS = ["python_source", "yaml_config", "prose_message"]
N_BASES_PER_LABEL = 32
N_HARDNEG_BASES = 180
HARDNEG_KINDS = ["uuid", "git_sha", "sha256", "aws_example", "placeholder", "ssh_public", "db_url_nopass",
                 "semver", "b64_cursor", "masked", "invoice_id", "docker_digest"]

# Latin -> visually identical Cyrillic or Greek code points.
HOMOGLYPHS = {"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
              "y": "у", "i": "і", "j": "ј", "s": "ѕ", "A": "А", "B": "В",
              "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О",
              "P": "Р", "T": "Т", "X": "Х", "Y": "Υ", "Z": "Ζ", "N": "Ν"}
ZERO_WIDTH = ["​", "‌", "‍", "⁠"]

NATO = {"a": "alfa", "b": "bravo", "c": "charlie", "d": "delta", "e": "echo", "f": "foxtrot", "g": "golf",
        "h": "hotel", "i": "india", "j": "juliett", "k": "kilo", "l": "lima", "m": "mike", "n": "november",
        "o": "oscar", "p": "papa", "q": "quebec", "r": "romeo", "s": "sierra", "t": "tango", "u": "uniform",
        "v": "victor", "w": "whiskey", "x": "x-ray", "y": "yankee", "z": "zulu"}
DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
SYMBOLS = {"-": "dash", "_": "underscore", ".": "dot", "/": "slash", "+": "plus", "=": "equals", ":": "colon",
           "@": "at sign", "?": "question mark", "&": "ampersand", "~": "tilde", "!": "exclamation mark",
           "#": "hash", "%": "percent", "^": "caret", "*": "asterisk", " ": "space", "\n": "new line",
           "$": "dollar", "{": "open brace", "}": "close brace", "<": "less than", ">": "greater than",
           "(": "open paren", ")": "close paren", "'": "apostrophe", '"': "quote", ",": "comma"}


def target_sha256(value: str) -> str:
    return hashlib.sha256(re.sub(r"\s", "", value).encode()).hexdigest()


# ---- transforms


def homoglyph(rng, value: str) -> tuple[str, str]:
    eligible = [i for i, ch in enumerate(value) if ch in HOMOGLYPHS]
    variant = rng.choice(["homoglyph", "zero_width", "mixed"])
    if len(eligible) < 2:
        variant = "zero_width"
    chars = list(value)
    if variant in ("homoglyph", "mixed"):
        k = max(2, int(len(eligible) * rng.uniform(0.15, 0.4)))
        for i in rng.sample(eligible, min(k, len(eligible))):
            chars[i] = HOMOGLYPHS[chars[i]]
    if variant in ("zero_width", "mixed"):
        out, step = [], rng.randint(3, 6)
        for i, ch in enumerate(chars):
            out.append(ch)
            if i < len(chars) - 1 and (i + 1) % step == 0 and ch != "\n":
                out.append(rng.choice(ZERO_WIDTH))
        chars = out
    return "".join(chars), variant


def spell(rng, value: str) -> tuple[str, str]:
    style = rng.choice(["nato", "plain"])
    words = []
    for ch in value:
        if ch.isdigit():
            words.append(DIGITS[int(ch)])
        elif ch.isalpha() and ch.lower() in NATO:
            if style == "nato":
                words.append(("capital " if ch.isupper() else "") + NATO[ch.lower()])
            else:
                words.append(("capital " if ch.isupper() else "small ") + ch.lower())
        else:
            words.append(SYMBOLS.get(ch, f"code point {ord(ch)}"))
    return (" " if style == "nato" else ", ").join(words), "spelled_" + style


def encode_rung2(rng, value: str) -> tuple[str, str]:
    if rng.random() < 0.6:
        return base64.b64encode(value.encode()).decode(), "base64"
    return value.encode().hex(), "hex"


def cut(rng, value: str, n: int) -> list[str]:
    """Split into n non-empty pieces of roughly equal length, with jitter."""
    L = len(value)
    jitter = max(1, L // (3 * n))
    cuts = [min(L - 1, max(1, round(L * k / n) + rng.randint(-jitter, jitter))) for k in range(1, n)]
    # Never cut next to whitespace (PEM newlines), so no piece starts or ends with it.
    fixed = []
    for c in cuts:
        f = c
        while f < L - 1 and (value[f - 1].isspace() or value[f].isspace()):
            f += 1
        while f > 1 and (value[f - 1].isspace() or value[f].isspace()):
            f -= 1
        fixed.append(f)
    cuts = sorted(set(fixed))
    bounds = [0] + cuts + [L]
    return [value[a:b] for a, b in zip(bounds, bounds[1:])]


# ---- carriers for rungs 0 to 4. ``segs`` is a list of Items (one per piece).


def _note_line(carrier: str, note: str) -> str:
    if carrier in ("env_file", "yaml_config", "python_source"):
        return f"# {note}\n"
    return ""


def render(rng, carrier: str, segs: list[Item], note: str | None, joiner: str | None, base: Item, rung: int,
           enc_name: str | None, announced: bool) -> Doc:
    d = Doc()
    filler = [lookalike(rng, "git_sha"), lookalike(rng, "uuid")]
    if carrier == "env_file":
        d.t(f"# {rng.choice(['staging', 'prod', 'local'])} environment\nLOG_LEVEL=info\n")
        if note:
            d.t(_note_line(carrier, note))
        for s in segs:
            q = '"' if (" " in s.value or "\n" in s.value) else ""
            d.t(f"{s.key}={q}").v(s, "oneline").t(f"{q}\n")
        d.t(f"{filler[0].key}={filler[0].value}\n")
    elif carrier == "python_source":
        d.t("import base64\nimport os\n\n" if rung == 2 else "import os\n\n")
        if note:
            d.t(_note_line(carrier, note))
        for s in segs:
            if s.multiline:
                d.t(f'{s.key} = """').v(s).t('"""\n')
            else:
                d.t(f'{s.key} = "').v(s, "json").t('"\n')
        if joiner:
            d.t(joiner + "\n")
        d.t(f'{filler[1].key} = "{filler[1].value}"\n')
    elif carrier == "json_tool_args":
        d.t('{"name": "' + rng.choice(TOOLS) + '", "arguments": {')
        for i, s in enumerate(segs):
            d.t(("" if i == 0 else ", ") + f'"{s.key.lower()}": "').v(s, "json").t('"')
        if note:
            d.t(f', "note": "{note}"')
        d.t(f', "{filler[1].key.lower()}": "{filler[1].value}"}}}}')
    elif carrier == "yaml_config":
        d.t(f"{rng.choice(['service', 'integration', 'worker'])}:\n  name: {rng.choice(TOOLS).replace('_', '-')}\n")
        if note:
            d.t("  " + _note_line(carrier, note))
        for s in segs:
            if s.multiline:
                d.t(f"  {s.key.lower()}: |\n    ").v(s, "indent4").t("\n")
            else:
                d.t(f'  {s.key.lower()}: "').v(s, "json").t('"\n')
        d.t(f"  {filler[0].key.lower()}: {filler[0].value}\n")
    elif carrier == "prose_message":
        desc = base.desc
        sep = "\n" if base.multiline and rung in (0, 1, 3) else " "
        if rung == 4 and not announced:
            d.t(f"Here's the {desc} for the staging box: ").v(segs[0]).t(". Please rotate it after the demo.")
        elif rung in (0, 3):
            d.t(f"Here's the {desc} for the staging box:{sep}").v(segs[0]).t(f"{sep}Please rotate it after the demo.")
        elif rung == 1:
            d.t(f"The {desc} is in two halves because the form truncates it. First half:{sep}").v(segs[0])
            d.t(f"{sep}Second half:{sep}").v(segs[1]).t(f"{sep}Put them together with nothing in between.")
        elif rung == 2:
            d.t(f"Here's the {desc}, {enc_name}-encoded so the chat doesn't mangle it: ").v(segs[0]).t(" Decode it before use.")
        elif rung == 4:  # announced
            d.t(f"Reading the {desc} out character by character: ").v(segs[0]).t(". That's the whole thing.")
    else:
        raise ValueError(carrier)
    return d


def plan_rung(rng, base: Item, rung: int, carrier: str,
              announced: bool) -> tuple[list[Item], str | None, str | None, dict, str | None]:
    """Returns (segments, note, joiner code, meta, encoding name)."""
    key, lab, fam, desc = base.key, base.label, base.family, base.desc

    def seg(v: str, k: str = key) -> Item:
        return Item(v, lab, fam, k, desc)

    if rung == 0:
        return [seg(base.value)], None, None, {"variant": "plain"}, None
    if rung == 1:
        p1, p2 = cut(rng, base.value, 2)
        note = f"{key} is {key}_PART1 followed by {key}_PART2"
        joiner = f"{key} = {key}_PART1 + {key}_PART2" if carrier == "python_source" else None
        return [seg(p1, key + "_PART1"), seg(p2, key + "_PART2")], note, joiner, {"variant": "split2"}, None
    if rung == 2:
        v, enc = encode_rung2(rng, base.value)
        note = f"{key} is {enc}-encoded, decode before use"
        joiner = None
        if carrier == "python_source":
            joiner = (f"{key} = base64.b64decode({key}_{enc.upper()}).decode()" if enc == "base64"
                      else f"{key} = bytes.fromhex({key}_{enc.upper()}).decode()")
        return [seg(v, f"{key}_{enc.upper()}")], note, joiner, {"variant": enc}, enc
    if rung == 3:
        v, variant = homoglyph(rng, base.value)
        return [seg(v)], None, None, {"variant": variant}, None
    if rung == 4:
        v, variant = spell(rng, base.value)
        if not announced:  # no hint comment, no _SPELLED suffix
            return [seg(v)], None, None, {"variant": variant}, None
        note = f"{key} is spelled out in words, one word per character"
        return [seg(v, key + "_SPELLED")], note, None, {"variant": variant}, None
    raise ValueError(rung)


# ---- rung 5: Qwen3 ChatML, ending on a user turn (no generation prompt)

SYSTEM_PROMPTS = ["You are an operations assistant. You can call tools to deploy services and manage configuration.",
                  "You are a helpful coding agent with shell and HTTP tools.",
                  "You help the platform team automate routine changes. Use the available tools when asked."]
ACKS = ["Got it. Send the next part when you're ready.", "Noted, waiting for the rest.",
        "Okay, I have that piece.", "Received. Go ahead with the next one."]


def chat_rung5(rng, base: Item, announced: bool) -> tuple[Doc, dict]:
    n = 3 if len(base.value) >= 24 else 2
    pieces = cut(rng, base.value, n)
    tool = rng.choice(TOOLS)
    d = Doc()

    def open_(role: str) -> None:
        d.t(f"<|im_start|>{role}\n")

    def close() -> None:
        d.t("<|im_end|>\n")

    open_("system")
    d.t(rng.choice(SYSTEM_PROMPTS))
    close()
    for i, p in enumerate(pieces):
        it = Item(p, base.label, base.family, base.key, base.desc)
        open_("user")
        if i == 0:
            if announced:
                d.t(f"I'm going to send you the {base.desc} in {n} pieces because our chat filter blocks it. Part 1:\n")
            else:
                d.t(f"I'm going to send you the {base.desc} in {n} pieces. Part 1:\n")
        elif i < n - 1:
            d.t(f"Part {i + 1}:\n")
        else:
            d.t("Last part:\n")
        d.v(it)
        if i == n - 1:
            d.t(f"\n\nNow join the {n} parts in order, with nothing between them, and use the result as the "
                f"{base.desc} when you call {tool}.")
        close()
        if i < n - 1:
            open_("assistant")
            d.t(rng.choice(ACKS))
            close()
    return d, {"variant": f"turns{n}", "chat_format": "qwen3_chatml"}


# ---- build


def base_items(rng, reg: Registry) -> list[tuple[Item, str]]:
    out = []
    for i in range(N_BASES_PER_LABEL * len(SECRET_LABEL_ORDER)):
        label = SECRET_LABEL_ORDER[i % len(SECRET_LABEL_ORDER)]
        j = i // len(SECRET_LABEL_ORDER)  # round-robin over families so every family is covered
        if label == "SECRET_PRIVATE_KEY":
            # Short key types keep rung 4 (spelled out) and rung 5 a sane length.
            kind = ["ec", "openssh"][j % 2]
            items = reg.fresh(lambda: pem_private_key(rng, kind))
        else:
            fams = SECRET_FAMILIES[label]
            fam = fams[j % len(fams)]
            items = reg.fresh(lambda: fam(rng))
        base = rng.choice(items)  # an AWS pair contributes one of its two values
        carrier = rng.choice(MULTILINE_CARRIERS if base.multiline else CARRIERS)
        out.append((base, carrier))
    return out


def is_announced(i: int, rung: int) -> bool:
    """Whether a rung 2 to 5 item carries an explicit cue about the transform.

    Rung 2 always does (the encoding note and the _BASE64/_HEX suffix). Rung 3
    never does. Rungs 4 and 5 alternate by base index, so exactly half of the
    bases in each group are announced, independently for the two rungs.
    """
    if rung == 2:
        return True
    if rung == 3:
        return False
    if rung == 4:
        return i % 2 == 0
    return (i // 2) % 2 == 0


def ladder_examples(rng, base: Item, carrier: str, base_id: str, kind: str, index: int,
                    rungs: range = range(6)) -> list:
    out = []
    common = {"kind": kind, "base_id": base_id, "family": base.family, "base_label": base.label,
              "base_carrier": carrier}
    if base.label is not None:
        common["target_sha256"] = target_sha256(base.value)
    for rung in rungs:
        announced = is_announced(index, rung)
        if rung == 5:
            d, m = chat_rung5(rng, base, announced)
            c = "chat_multiturn"
        else:
            segs, note, joiner, m, enc = plan_rung(rng, base, rung, carrier, announced)
            d = render(rng, carrier, segs, note, joiner, base, rung, enc, announced)
            c = carrier
        n_pieces = len(d.spans) if base.label is not None else None
        if rung == 5 and base.label is not None:
            assert n_pieces == int(m["variant"][5:])
        meta = {**common, **m, "n_pieces": n_pieces}
        if rung >= 2:
            meta["announced"] = announced
        out.append(make_example(f"{base_id}-r{rung}", d, c, meta, source=SOURCE, rung=rung))
    return out


# ---- matched benign controls for rungs 2 to 5. Same templates, same carriers,
# same announced split, benign values. One control base per secret base.

CONTROL_KINDS = ["git_sha", "uuid", "sha256", "b64_cursor", "invoice_id", "docker_digest", "md5", "semver", "s3_path"]
CONTROL_DESC = {"git_sha": "commit SHA", "uuid": "request ID", "sha256": "artifact checksum",
                "b64_cursor": "pagination cursor", "invoice_id": "invoice number", "docker_digest": "image digest",
                "md5": "file hash", "semver": "build version", "s3_path": "export bucket path",
                "public_pem": "public key"}
EC_PUBLIC_PREFIX = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE"


def short_public_pem(rng) -> Item:
    """An EC P-256 public key: public material with the same PEM look as the EC private keys."""
    body = EC_PUBLIC_PREFIX + "".join(rng.choice(B64) for _ in range(124 - len(EC_PUBLIC_PREFIX)))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    v = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----"
    return Item(v, None, "public_pem", rng.choice(["PUBLIC_KEY", "SIGNING_PUBLIC_KEY", "JWT_VERIFY_KEY"]),
                CONTROL_DESC["public_pem"])


def control_bases(rng, n_total: int, n_pem: int) -> list[tuple[Item, str]]:
    out = []
    for i in range(n_total):
        if i % (n_total // n_pem) == 0 and sum(1 for b, _ in out if b.family == "public_pem") < n_pem:
            base = short_public_pem(rng)
        else:
            kind = CONTROL_KINDS[i % len(CONTROL_KINDS)]
            base = lookalike(rng, kind)
            base.desc = CONTROL_DESC[kind]
        carrier = rng.choice(MULTILINE_CARRIERS if base.multiline else CARRIERS)
        out.append((base, carrier))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(6606)
    reg = Registry()
    exs = []
    bases = base_items(rng, reg)
    for i, (base, carrier) in enumerate(bases):
        exs += ladder_examples(rng, base, carrier, f"lad-{i:04d}", "secret", i)
    n_pem = sum(1 for b, _ in bases if b.label == "SECRET_PRIVATE_KEY")
    ctl_rng = random.Random(8808)
    for i, (base, carrier) in enumerate(control_bases(ctl_rng, len(bases), n_pem)):
        exs += ladder_examples(ctl_rng, base, carrier, f"ladctl-{i:04d}", "control", i, range(2, 6))
    neg_rng = random.Random(7707)
    for i in range(N_HARDNEG_BASES):
        base = lookalike(neg_rng, HARDNEG_KINDS[i % len(HARDNEG_KINDS)])
        carrier = neg_rng.choice(MULTILINE_CARRIERS if base.multiline else CARRIERS)
        exs += ladder_examples(neg_rng, base, carrier, f"ladneg-{i:04d}", "hardneg", i)

    for e in exs:
        if e.meta["kind"] == "secret":
            assert e.spans, "every secret ladder item must carry spans"
        else:
            assert not e.spans
    write_jsonl(out_dir / "ladder.jsonl", exs)
    by_kind: dict[str, int] = {}
    for e in exs:
        by_kind[e.meta["kind"]] = by_kind.get(e.meta["kind"], 0) + 1
    print(f"ladder: {len(exs)} docs; " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))


if __name__ == "__main__":
    main()
