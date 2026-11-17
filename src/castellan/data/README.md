# castellan.data

- `schema.py`: the JSONL example format.
- `synth.py`: synthetic training data (secrets, PII, hard negatives).
- `lookalikes.py`: benign documents that look like secrets but are not. All
  negative, no spans. Generate them with `castellan synth --lookalikes N`.
- `convert.py`: third-party span datasets to our schema.

## Lookalikes

Categories, each built from false-positive classes that secret scanners
publish (full citations are in each category's docstring in `lookalikes.py`):

| Category | Main sources |
| --- | --- |
| `placeholder` | gitleaks v8.30.1 global allowlist regexes; `generic-api-key` and `curl-auth-user` allowlists; detect-secrets `is_templated_secret`, `is_prefixed_with_dollar_sign` |
| `doc_example` | AWS IAM and AWS CLI docs example keys; gitleaks `aws-access-token` allowlist `.+EXAMPLE$`; gitleaks `gcp-api-key` allowlist; RFC 7519 section 3.1 |
| `indirect_reference` | detect-secrets `is_indirect_reference`; AWS Secrets Manager ARN format; Vault `kv get` docs; gitleaks `--mount=type=secret,` line allowlist |
| `masked` | gitleaks global repeated-char allowlist, `curl-auth-user` allowlist; detect-secrets `is_not_alphanumeric_string`; gitleaks `--redact` output |
| `public_key` | gitleaks `private-key` rule (matches PRIVATE KEY only); `generic-api-key` allowlist `pub(?:lic)?[_.-]?key` |
| `hash_digest` | gitleaks global paths `go.sum`, `verification-metadata.xml`; `generic-api-key` allowlist `issuerkeyhash` |
| `uuid_id` | detect-secrets `is_potential_uuid`, `is_likely_id_string`; gitleaks global UUID stopword and `generic-api-key` id-name allowlist |
| `sequential` | detect-secrets `is_sequential_string`; gitleaks global repeated-char regex and `abcdefghijklmnopqrstuvwxyz` stopword |
| `lockfile` | gitleaks global lock-file paths; detect-secrets `is_lock_file` |
| `benign_base64` | RFC 2397 data URIs; Slack cursor-based pagination docs; gitleaks image path allowlist |

### Excluded (could not be sure they are not credentials)

These appear in the sources but were left out on purpose:

- **Stripe and other `sk_test_` / `pk_test_` keys.** Test-mode keys are real
  credentials for a real account.
- **Every `gcp-api-key` allowlist entry in gitleaks except
  `AIzaSyabcdefghijklmnopqrstuvwxyz1234567`.** The others look like real
  browser or app keys that were published in public code. They are allowlisted
  because they are public, not because they are fake.
- **The Azurite / Azure Storage Emulator well-known account key.** It is
  documented, but it still authenticates to a running emulator.
- **The `nuget-config-password` allowlist values in gitleaks** (`33f!!lloppa`,
  `hal+9ooo_da!sY`). Their origin is not documented in the config, so we
  cannot tell whether they were ever live passwords.
- **The `vault-service-token` allowlist regex `s\.[A-Za-z]{24}`.** Its intent
  is not documented, and the shape is a real Vault token shape.
- **Slack's documented `xoxp-111-222-333-...` example tokens.** They look fake,
  but they carry a vendor prefix, and we did not confirm which doc section
  publishes them as examples, so they stay out.
- **Sequential or repeated values under password names.** detect-secrets drops
  these, but `password: 123456` is still a (weak) password, so the
  `sequential` category only uses neutral key names.
