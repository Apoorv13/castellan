# eval_data: frozen eval sets (M2)

These eval sets are frozen. Do not edit any file listed in `FROZEN.sha256`; check with `shasum -a 256 -c eval_data/FROZEN.sha256` from the repo root. Training code must never import or read anything in this directory (docs/DESIGN.md section 8).

## Regenerate

```bash
python eval_data/secrets_eval.py   # secrets_eval, dev, final, hardneg, longctx
python eval_data/ladder.py         # ladder
```

Both scripts are deterministic, with one fixed seed per set, and write byte-identical files on every run. This was checked by generating twice and comparing sha256. They need only the standard library plus `castellan.data.schema` for validation and JSONL writing. They run offline in about a second, and they print counts only, never values.

## Clean-room note

Both generators were written in a session that did not open `src/castellan/data/` or `src/castellan/train.py`. The carriers, templates, filler and lookalike pools are designed separately from the training generator. Overlap in the general kind of carrier (JSON, YAML, env files) is unavoidable. The specific templates, field names and phrasing are independent.

## Files

| File | Docs | With spans | Without spans | Seed | Purpose |
| --- | --- | --- | --- | --- | --- |
| `secrets_eval.jsonl` | 1400 | 1000 | 400 | 1101 | Secrets across all 7 classes and all 20 carriers (70 docs per carrier). About 25% of positives also carry one PII value. |
| `dev.jsonl` | 1050 | 750 | 300 | 2202 | Use this to choose between experiments. 450 secret docs, 300 PII docs (pattern PII about 65%), 300 negatives. |
| `final.jsonl` | 1050 | 750 | 300 | 3303 | Held out. Same recipe as dev with a different seed. Never use it to choose anything. |
| `hardneg.jsonl` | 800 | 0 | 800 | 4404 | Benign lookalikes only (40 per carrier). |
| `longctx.jsonl` | 150 | 90 | 60 | 5505 | 1 to 3 secrets buried in benign filler. |
| `ladder.jsonl` | 3320 | 1344 | 1976 | 6606, 7707 and 8808 | 224 secret bases and 180 lookalike bases at rungs 0 to 5, plus 224 matched benign control bases at rungs 2 to 5. |

Every doc in `secrets_eval`, `dev`, `final` and `hardneg` has a different seed-driven text. A cross-file check found no sensitive value shared between any two files, and no duplicate text across files. The only exceptions are `HEALTH` and `CONTEXTUAL` phrases, which come from small fixed pools and do repeat. The ladder has 7 duplicate texts, all negatives built from fixed or small-pool values: the AWS example keys, a short placeholder, and the base64 cursors. In one of them, a very short placeholder had no room for a zero-width insert, so its rung 3 text equals its rung 0 text.

**longctx token counts** (Qwen3-0.6B tokenizer, measured): min 8,337, max 29,109. The filler is procedural: prose paragraphs, Python functions, log blocks, markdown tables and YAML. `meta.depths` gives each secret's relative position in the doc.

## Schema use

- `source` is `eval:secrets_eval` or `eval:ladder`. `rung` is `null` except in the ladder.
- `meta.kind` is `positive`, `positive_pii`, `negative` or `hardneg` (ladder: `secret`, `hardneg` or `control`). Ladder rung 2 to 5 items also carry `meta.announced`.
- `meta.families` lists the specific formats in a doc, for example `github_token` or `pem_ec`. Use it for per-format breakdowns below the class level.

## Span conventions

These apply the same way in every file.

- A span covers the value only. It never includes the surrounding quotes, the key name, `Bearer `, or the `=`.
- **Escaped values.** When a carrier escapes the value (a PEM inside a JSON string, a `.env` line, or a notebook cell), the span covers the escaped form exactly as it appears in the text. Example: `\n` as two characters, or `\\n` inside a JSON-in-JSON tool result.
- **YAML block scalars.** For a PEM in a YAML `|` block, the span runs from `-----BEGIN` to the final `-----` and includes the indentation inside it.
- `SECRET_DB_URL` covers the whole connection URL, from the scheme to the end of the query string.
- `SECRET_PRIVATE_KEY` covers the full PEM block, from `-----BEGIN ...-----` to `-----END ...-----`.
- An AWS key pair gives two `SECRET_CLOUD_KEY` spans: the access key id and the secret key.
- **Field names match the value.** A `MONGO_URI` always holds a mongodb URL, `REDIS_URL` a redis URL, `SQLALCHEMY_DATABASE_URI` a SQL URL. An OpenSSH key is never named `TLS_PRIVATE_KEY`, `xoxp-` is a Slack user token, `rk_live_` is a Stripe restricted key, and only `ghp_` can be named `GITHUB_PAT`. A check over every rung-0 and non-ladder span found no mismatches.
- `CARD` keeps its separators, such as spaces or dashes. `ADDRESS` covers street, city, state and ZIP. `HEALTH` and `CONTEXTUAL` cover the identifying phrase.
- **Ladder rung 1 and rung 5.** Each piece gets its own span with the base label. Joiner code, notes and chat scaffolding are never inside a span.
- **Ladder rung 3.** The span includes the inserted zero-width characters and the homoglyph code points. Offsets are code-point indices, matching the schema.

## Carriers (20, plus 2 special ones)

`mcp_tools_call` (JSON-RPC tools/call), `agent_react` (Thought/Action/Observation trace), `python_module`, `node_config`, `compose_yaml`, `k8s_manifest` (Secret stringData), `terraform` (tfvars, heredoc for PEM), `git_diff` (a unified diff adding env lines), `systemd_unit`, `ini_profile`, `notebook_json` (a Python string inside a JSON notebook cell), `sql_insert`, `jsonl_log`, `curl_command`, `markdown_howto`, `crm_tool_result` (MCP tool result with JSON double-encoded in a text field), `csv_export`, `chat_paste`, `email_body`, `support_ticket`.

`longctx` uses `long_mixed`. Ladder rung 5 uses `chat_multiturn`. Ladder rungs 0 to 4 use `env_file`, `python_source`, `json_tool_args`, `yaml_config` and `prose_message`.

## Secret formats

Every value follows a public, documented shape: the prefix, length and alphabet. The body is random, so no value is a working credential.

- **CLOUD:** AWS key id plus secret pair, GCP `AIza`, Azure storage key, DigitalOcean `dop_v1_`, Alibaba `LTAI`, Cloudflare token
- **AI:** OpenAI `sk-proj-`/`sk-svcacct-` with `T3BlbkFJ`, Anthropic `sk-ant-api03-`, HF `hf_`, Groq `gsk_`, Replicate `r8_`, Perplexity `pplx-`, Mistral (bare 32 chars)
- **VCS:** `ghp_`/`gho_`/`ghs_`/`ghu_`, `github_pat_11`, `glpat-`, `glptt-`, Bitbucket `ATBB`
- **JWT:** HS256, RS256 or ES256, with a real base64url JSON header and payload and a random signature
- **PRIVATE_KEY:** PEM RSA, PKCS8, EC and OpenSSH, each with its real DER or OpenSSH header prefix
- **DB_URL:** postgres(ql), mysql, mongodb+srv, redis, amqp and mssql URLs with a password
- **GENERIC:** Slack `xoxb-`/`xoxp-`, Stripe `sk_live_`/`rk_live_`, SendGrid `SG.`, Twilio `SK`, npm `npm_`, Mailgun `key-`, Shopify `shpat_`, Datadog (bare hex), plain passwords

Bare formats (Mistral, Datadog, Cloudflare, plain passwords) are only identifiable from context. They are there on purpose.

## PII

All PII is synthetic:
- Phones use the 555-01xx and UK 020 7946 0xxx drama ranges.
- IPs are mostly from the documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24, 2001:db8::/32), plus some public-looking addresses.
- Emails use only `example.com`, `example.org` and `example.net`. These are RFC 2606 reserved names that sit on real public suffixes. An earlier draft also used `.test` and `corp.example`, but Presidio's email recognizer validates the TLD and rejected all of them (0 of 56 in dev). That would have handicapped the baseline for an artifact reason, so they were removed before the freeze.
- Card numbers are random and Luhn-valid.
- US SSNs avoid area 000 and 666 and the 9xx range. There are also UK NINO-shaped values.
- Names and addresses are combined from hand-written lists.

## Hard negatives

**The rule:** an opaque random value (UUID, git SHA, sha256, md5, docker digest) only ever appears under a clearly non-secret name, such as `REQUEST_ID` or `COMMIT_SHA`. Secret-sounding names (`OPENAI_API_KEY`, `DATABASE_URL`, `GITHUB_TOKEN`) only ever hold values that are clearly not live secrets:
- the AWS documentation example keys
- placeholders (`<YOUR_API_KEY>`, `${{ secrets.GITHUB_TOKEN }}`, `ghp_xxxx...`)
- masked values (`sk-proj-****abcd`)
- DB URLs with no password
- public keys and certificates (`ssh-ed25519`, `BEGIN PUBLIC KEY`, `BEGIN CERTIFICATE`)

Other lookalikes include base64 pagination cursors, npm integrity hashes, Luhn-failing 16-digit tracking numbers, invoice ids, semvers and S3 paths. The same lookalikes also appear as filler next to real secrets in `secrets_eval`, `dev` and `final`. There they are what actually tests in-context precision.

## Ladder

- **Bases.** 32 bases per secret class, round-robin over the formats in each class so every format appears (2 to 16 bases each). An AWS pair contributes one of its two values. Private-key bases use only EC and OpenSSH keys, so the spelled-out rung stays a sane length. Multi-line bases use only carriers that can hold raw newlines (Python triple quotes, a YAML block, prose).
- **Rung 1.** A random cut in the middle third, moved off any whitespace so no piece starts or ends with a newline, written as `KEY_PART1`/`KEY_PART2` with a note or joiner line.
- **Rung 2.** base64 (about 60%) or hex, with a note or a decode line.
- **Rung 3.** One of three variants: Cyrillic or Greek homoglyphs (15 to 40% of the eligible characters), zero-width characters (U+200B, U+200C, U+200D or U+2060 every 3 to 6 characters), or both mixed. The variant is in `meta.variant`.
- **Rung 4.** Each character becomes words, in one of two styles: NATO style ("capital kilo seven dash") or plain style ("capital k, seven, dash").
- **Rung 5.** The secret is cut into 2 pieces (values under 24 chars) or 3 near-equal pieces, again never next to whitespace. The conversation is rendered as Qwen3 ChatML: system, then user and assistant turns alternating, ending on a user turn that asks to join the pieces. There is no generation prompt. All 628 rung-5 texts (secrets, hard negatives and controls) match `apply_chat_template(..., tokenize=False)` from the Qwen3-0.6B tokenizer exactly.
- **Readability.** `meta.target_sha256` is the sha256 of the plain base value with all whitespace removed. `readability.py` can normalize the model output the same way, hash it and compare, so the value is never stored twice or logged. `meta.base_id` links all 6 rungs of a base.
- **Hard-negative bases.** 180 bases, 15 for each of 12 lookalike kinds, go through the same transforms with no spans. That gives 180 negatives per rung.
- **Announced vs unannounced (`meta.announced`, rungs 2 to 5).** Templates can carry cues that a detector could key on without recognizing the secret, so the cue is recorded and split:
  - Rung 2 is always announced (the encoding note and the `_BASE64`/`_HEX` suffix).
  - Rung 3 is never announced (nothing in the text says the value was disguised).
  - Rungs 4 and 5 are announced for exactly half of the bases, chosen by base index and independently for the two rungs. Announced rung 4 has the "spelled out in words" comment and the `_SPELLED` suffix. Unannounced rung 4 has neither and uses the plain rung-0 field name or sentence. Announced rung 5 says "because our chat filter blocks it". Unannounced rung 5 drops that line but keeps the join instruction.
- **Matched benign controls (`meta.kind = control`, rungs 2 to 5).** One control base per secret base, so 224 controls per rung against 224 secrets. Controls use the same templates, carriers, cut logic and announced split, with benign values: commit SHAs, request IDs (UUIDs), checksums, base64 cursors, invoice numbers, image digests, file hashes, build versions and bucket paths. The 32 private-key bases are matched by 32 short EC public keys (`BEGIN PUBLIC KEY`), which go only into the multi-line carriers, so the spelled-out and split PEM templates have a benign twin. Controls carry no spans and `rung` is set.
- **Rung 4 PEM share.** Private keys are 32 of 224 rung-4 secret items (14.3%) but 33.6% of rung-4 secret span characters, because a spelled EC or OpenSSH key runs to thousands of characters. Report rung 4 per class, or weight by item, so the PEM items do not dominate char-level numbers.

## Notes for the user

1. **The hardneg gate is now defined on false-positive rates.** Char precision is undefined on a set with no gold spans, so docs/DESIGN.md section 7 was changed on main (239cedc) to gate on `hardneg` with labels ignored: char FPR at most 0.005 and document flag rate at most 0.05.
2. **Default Gitleaks 8.30.1 breaks the section 6 expectations at rungs 1 and 2.** These numbers come from a doc-level sanity pass that was run only to check the design. They are measurements, and nothing was changed because of them.
   - Rung 2: Gitleaks decodes base64 and hex by default (`--max-decode-depth 5`), so it caught 179 of 224 secret items.
   - Rung 1: its generic high-entropy rule fires on the split pieces, so it caught 137 of 224.
   - Rung 0: 189 of 224. Rungs 3 and 4: 0. Rung 5: 32.
   - Negatives: hard negatives had 6 and 10 hits at rungs 1 and 2 and none elsewhere. Controls had 4 hits at rung 2 and none elsewhere.
3. **Gitleaks defaults have no rule for credentials in connection URLs.** In a quick sanity pass on `secrets_eval`, the single-family `SECRET_DB_URL` docs had 0 hits, and plain passwords had 1 in 9. So rung-0 recall for those classes is low for Gitleaks by construction. I did not change anything in response to this.
4. **Presidio defaults have two gaps on valid formats.** Both are measured with the pattern recognizers on dev, and the data was left as is.
   - Compressed IPv6 such as `2001:db8:1a2b:3c4d::5e6` matches only partially, so it counts on overlap but not on exact boundaries.
   - `UkNinoRecognizer` ships with Presidio but is not in the default registry, so UK NINO values (about a quarter of `GOV_ID`) get 0 recall under the stock defaults. The baseline wrapper on main (a651e64) now adds it.
   - Every other pattern-PII format in dev is matched in full: cards, all four phone formats, SSNs, IPv4 and emails.
5. **dev and final share one recipe**, so final is an unbiased check of choices made on dev. `secrets_eval` uses the same carriers with a different class balance.
6. **What the controls measure, and what the neutral set measures.** The ladder controls measure false positives on benign values in the same templates. They do not measure reliance on name cues: a secret is still called a "GitHub token" or sits in `GITHUB_PAT`, while a control is a "commit SHA" in `BUILD_COMMIT`. Name-cue reliance is measured by `ladder_neutral.jsonl` (below).

## Addendum: ladder_neutral.jsonl (frozen separately)

```bash
python eval_data/ladder_neutral.py   # ladder_neutral
```

- **Purpose.** Removes name cues, so probe recall on these secret items can be compared with the same rungs in `ladder.jsonl`. The gap is the reliance on field names and descriptions.
- **Contents.** 60 secret bases (8 or 9 per class, round-robin over formats) and 60 matched controls, each at rungs 0, 3, 4 and 5. That is 480 docs: 240 secret items and 240 controls. `source` is `eval:ladder_neutral` and `meta.neutral` is true.
- **Neutral names.** The target always sits in `value`, `arg1`, `data` or `field_3` (in `meta.field`), and prose and chat call it "the value".
  - Tool names (`process_record`, `submit_form` and so on), section names, system prompts and the filler field (`count`, `mode`, `index`, `status`) say nothing about secrets.
  - A scan of all text outside spans found no words such as secret, token, password, key, credential, auth, rotate, filter, or any vendor or database name.
- **Matching.** Each control shares its secret's carrier, field name and per-base random seed, so the template choices line up. Only the value differs. Private-key bases are matched by short EC public keys in the same multi-line carriers.
- **No announcements.** Rungs 3 to 5 are all unannounced (`meta.announced` is false). There are no hint comments or suffixes, and no "filter blocks it" line. Rung 5 keeps the join instruction.
- **Checks.** Two runs give identical bytes. No value or text is shared with any other eval file. No span starts or ends with whitespace. All 120 rung-5 chats match the Qwen3 chat template.
- **Small sample: report confidence intervals.** There are only 60 bases per arm (secret and control), so every neutral-set result must be reported with a bootstrap 95% confidence interval. Resample bases, not items, so the four rungs of a base stay together. Without intervals, a few bases can move a rate by several points, and a gap against `ladder.jsonl` can't be read.
- **Freeze.** The file and its generator are hashed in `FROZEN_ADDENDUM.sha256`. Verify with `shasum -a 256 -c eval_data/FROZEN_ADDENDUM.sha256` from the repo root. The generator imports the frozen `ladder.py` and `secrets_eval.py` and does not change them.
