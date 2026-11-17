# CASTELLAN: design, rules and milestones

This is the project brief that the work followed. Section numbers are stable: `EXPERIMENTS.md`, `STATE.md` and code comments cite them (for example "section 8", or "section 7 gate"). Progress is tracked in `STATE.md` and results are logged in `EXPERIMENTS.md`.

## 1. What CASTELLAN is

CASTELLAN is an open-source detector that finds secrets and PII in LLM and agent traffic by reading a language model's residual-stream activations with small trained probes. It returns character spans, class labels and calibrated scores.

**Research question (Frontier 3).** As a secret is disguised past regex scanners (split, encoded, homoglyphs, spelled out, reassembled across turns), does the probe keep flagging it for as long as the model itself can still read it?

**Why this angle.** Probe-based PII detection is already published (Goodfire with Rakuten, NeuroFilter), and so is probing for the *encoding computation* (arXiv 2606.10304). Nobody has measured probe recall on the secret itself across an obfuscation ladder, compared it with regex scanners, and related it to whether the model can read the secret.

**Later use (Frontier 1, separate project).** The probe becomes the classifier that decides KV-cache sharing inside a serving engine. Keep the probe API clean so it can be called from inside a forward pass later.

## 2. Ground rules

- **Clean room.** Use only public papers, public datasets and synthetic data.
- **Never print or log matched sensitive values**, even synthetic ones. Findings carry class, score, JSON path and offsets only. CLI output hides values unless `--show-values` is passed.
- **No real credentials, ever.** Generate every secret synthetically with valid-looking structure. Never fetch real leaked keys.
- **Single tenant, one machine.** Multi-tenant work and serving-engine integration are out of scope here.
- **No em-dashes** in code comments, docs or prose.

## 3. Machine

- Apple Silicon, 48 GB unified memory. See "Reproducibility" in `README.md`.
- Use `--device mps --dtype bf16`. There is no CUDA. If outputs come back NaN, fall back to fp32 for that model or op, and log it.
- Run long jobs in tmux, with the machine kept awake.

## 4. Architecture

```
castellan/
  pyproject.toml            # core: numpy, scikit-learn; extras: [torch], [presidio], [server], [all]
  src/castellan/
    extractor.py            # residual capture via forward hooks
    cache.py                # disk-backed activation cache (memmap)
    probe.py                # probe heads, standardization, calibration, thresholds
    spans.py                # token scores -> char spans
    bundle.py               # saved probe pinned to model id + revision + dtype
    train.py                # cache -> layer sweep -> fit -> calibrate -> threshold
    evaluate.py             # metrics + baselines (frozen once written)
    baselines/
      presidio.py           # Presidio + UK NINO baseline (Presidio analyzer wrapper)
      gitleaks.py           # gitleaks CLI wrapper -> char spans
    readability.py          # model-can-read scorer for ladder items
    data/
      schema.py             # JSONL example format
      synth.py              # TRAINING data generator
      convert.py            # third-party span datasets -> our schema (YAML label maps)
    guard.py                # agent tool-call guard: allow / redact / block
    cli.py                  # castellan synth | train | eval | scan | ladder | readability
  eval_data/                # FROZEN eval set generators and hashes (see integrity rules)
    secrets_eval.py
    ladder.py
    FROZEN.sha256
  maps/                     # label maps for converted datasets
  tests/                    # offline, CPU, tiny random model
  STATE.md  EXPERIMENTS.md
```

### Data schema (JSONL, one example per line)
```json
{"id": "...", "text": "...", "spans": [{"start": 10, "end": 42, "label": "SECRET_CLOUD_KEY"}],
 "carrier": "tool_call_json", "source": "synth|real:<dataset>|eval:<generator>", "rung": null, "meta": {}}
```
For multi-turn items, `text` is the chat-templated conversation, so offsets index into exactly what the model sees.

### Label set
- Secrets: `SECRET_CLOUD_KEY`, `SECRET_AI_KEY`, `SECRET_VCS_TOKEN`, `SECRET_JWT`, `SECRET_PRIVATE_KEY`, `SECRET_DB_URL`, `SECRET_GENERIC`
- Pattern PII: `EMAIL`, `PHONE`, `CARD`, `GOV_ID`, `IP_ADDRESS`
- Contextual PII: `PERSON`, `ADDRESS`, `CONTEXTUAL` (identifying only in context), `HEALTH`

Secrets are the priority for this project. PII support is kept so the Presidio + UK NINO comparison is fair, but it is not optimized.

### Design decisions
1. **Hooks and early stop.** Register forward hooks on decoder-block outputs only for the layers you need, and stop the forward pass after the deepest one. Layer k means `model.model.layers[k]` output, which equals `output_hidden_states[k+1]`.
2. **Windowing.** Long inputs go through overlapping windows. Merge per-token scores by taking the max in overlaps.
3. **Probe.** Per-class logistic heads on standardized activations, Platt calibration on a validation split, and per-class thresholds chosen for a target recall. Use a threshold floor of 0.05, and watch for a weak probe flooding spans.
4. **Spans.** Use hysteresis (a high threshold to start a span, a low one to continue it), then snap to word boundaries. **Bridge whitespace and punctuation tokens inside a span.** Without this, multi-token values like card numbers and keys fragment because whitespace tokens score low. A regression test covers it.
5. **Document score** = max of rolling means of token scores, not the plain max.
6. **Bundles** pin model id, revision and dtype. Loading a bundle against a different revision raises `RevisionMismatch`.
7. **Causal blind spot.** A token can't see context that comes after it. A linear probe can't use "the next line says this is a fake key." This is a limitation. An attention probe over the window can fix it later (Milestone 4).
8. **Planted-signal test.** Build a tiny random Qwen3-architecture model plus a small locally trained tokenizer. Plant a known direction in the residual stream for labeled tokens, and check that the full pipeline recovers it. This is the end-to-end correctness test, and it caught the whitespace bug.

## 5. Milestones

### M0: scaffold
- [x] `pyproject.toml`, package layout, venv, `pip install -e ".[all]"`
- [x] Schema and JSONL IO, plus tests
- [x] Tiny random model fixture and the planted-signal test
- [x] STATE.md with the machine specs and a "decisions" log

### M1: detector and baselines
- [x] `extractor.py` and `cache.py`: the disk-backed memmap cache, keyed by (model id, revision, dtype, layer, dataset hash), with a JSON index. Training reads shuffled mini-batches from disk. (An 8B sweep is 1M tokens x 8 layers x 4096 x 2 bytes, about 65 GB, so RAM caching won't work.)
- [x] `probe.py`, `spans.py`, `bundle.py`, `train.py` with a layer sweep (8 evenly spaced layers, chosen by validation AUROC, then refit at the best layer)
- [x] `evaluate.py`, with all the metrics in section 7
- [x] Baselines: Presidio + UK NINO (en_core_web_lg, stock registry plus UkNinoRecognizer) and Gitleaks (default rules, version recorded). Recorded: gitleaks 8.30.1, presidio-analyzer 2.2.362 with en_core_web_lg 3.8.0.
- [x] `data/synth.py`: training data across carriers (tool-call JSON args and results, shell, `.env`/YAML/TOML, logs, stack traces, HTTP headers, connection strings, CI config, prose) plus hard negatives (UUIDs, git SHAs, hashes, documented example keys, placeholders, test fixtures, benign base64)
- [x] Real PII data: ai4privacy was skipped for training because its license is unclear (2026-11-15). PII stays synthetic, so the weights are trained on synthetic data only. Licenses are recorded in EXPERIMENTS.md.
- [x] `guard.py` and `cli.py` (scan, train, eval)
- [x] Planted-signal test passes; all tests offline and green

### M2: frozen eval sets
Written **after** M1's training generator existed, with different templates and carriers, in a separate session that had not read `data/synth.py`.
- [x] `eval_data/secrets_eval.py` -> `secrets_eval.jsonl`
- [x] `eval_data/hardneg.jsonl`: benign lookalikes only
- [x] `eval_data/longctx.jsonl`: secrets buried in 8k to 32k tokens of benign filler
- [x] `eval_data/dev.jsonl` (used to choose between experiments) and `final.jsonl` (held out, never used to choose)
- [x] `eval_data/ladder.py` -> `ladder.jsonl` (section 6)
- [x] The SHA-256 of every eval file, both generators, and `src/castellan/evaluate.py` written to `FROZEN.sha256` and committed (see `PROVENANCE.md`)

### M3: the experiment
- [x] `readability.py`: for each ladder item, prompt the model (chat template, greedy) to output the reconstructed secret, and score an exact match after normalization. Store `model_can_read` per (item, model). Never log the model's output.
- [x] Train and evaluate Qwen3-0.6B, 1.7B, 4B and 8B (bf16, revisions pinned)
- [x] The headline table and plot: rung x model size, showing probe recall, Gitleaks recall, Presidio + UK NINO recall and the model_can_read rate. The key plot is probe recall vs model_can_read. (Done 2026-11-17. Recall is reported at 5% matched-control FPR instead of 1%: 1% is 1 or 2 controls per rung, below the resolution of the control sets.)
- [ ] Probe architecture comparison at the best layer: linear vs max-pool vs attention probe (deferred to M4, 2026-11-17)
- [x] Results summary in EXPERIMENTS.md, in plain language

### M4: later
Robustness attacks on the probe (text-only attacker), then the attention probe for the causal blind spot, then the Frontier 1 engine hook.

## 6. Obfuscation ladder

`eval_data/ladder.py` takes a base secret example and emits one variant per rung. Each variant keeps the char spans of every piece of secret material and stores `rung` on the example.

| Rung | Transform | Regex scanner expected to... |
| --- | --- | --- |
| 0 | Plain, in a realistic carrier | catch it |
| 1 | Split across two variables or JSON fields | miss it |
| 2 | base64 or hex encoded | miss it |
| 3 | Homoglyphs or zero-width characters inside the secret | miss it |
| 4 | Spelled out in words | miss it |
| 5 | Pieces spread across conversation turns, with an instruction to join them | miss it |

200 or more base secrets across all families and carriers. Ladder versions of the hard negatives (base64 of a UUID, a spelled-out placeholder) measure false positives per rung.

## 7. Metrics (every run, every eval set)

- Span precision, recall and F2, plus exact-boundary recall
- Char-level precision and recall: the redaction-quality measure, reported alongside the span metrics
- Document AUROC **and** recall at 1% FPR. Never report AUROC alone (one paper found a 9B classifier at AUROC 0.94 with only 0.30 TPR at 1% FPR).
- Per class, per carrier, per ladder rung
- Latency per 1k tokens on MPS, and peak memory

### Gate before the M3 runs count
On `dev` and `secrets_eval`, the probe on Qwen3-1.7B must:
- beat Gitleaks defaults on secrets recall at equal or better precision, without losing rung-0 recall
- (non-blocking, information only) beat Presidio + UK NINO on pattern-PII recall at equal precision. Made non-blocking on 2026-11-16: this baseline's pattern-PII recall is 1.0 on dev and secrets_eval, so the line cannot be met by any detector, and swapping in a matched-operating-point test after seeing the results would be post hoc. The probe's FPR at the baseline's recall is reported as information only.
- on `hardneg` (benign lookalikes, no gold spans), with labels ignored (the `any_label` block of the eval report):
  - `char_fpr` (predicted characters over all characters) of at most 0.005
  - `neg_doc_flag_rate` (fraction of hardneg documents with any span) of at most 0.05
  These were locked on 2026-11-15, before the M2 freeze. They replace "char precision of at least 0.9", which is undefined on a set with no gold spans.

The ladder is a measurement, not a gate. Never tune anything against it.

## 8. Integrity rules (break one and the run is invalid)

- Never edit anything listed in `FROZEN.sha256`. If `evaluate.py` truly needs a fix, log why, re-hash, and re-run every baseline.
- Never train, choose layers, calibrate or set thresholds on `final`, `hardneg`, `longctx`, `secrets_eval` or `ladder`. Validation splits come from training data only. `dev` is for choosing between experiments.
- Training code must not import or read `eval_data/*.py`. Don't write training negatives by copying the eval generators' logic.
- If a metric jumps after a change that only touched `data/synth.py`, flag it in EXPERIMENTS.md and investigate before trusting anything after it.
- Every EXPERIMENTS.md row records the git SHA, model id and revision, layer, data hash and seed.

## 9. Autonomous loop (only after M2 is frozen)

Once the eval sets are frozen and the baselines are recorded, an improve-and-evaluate loop may run on the probe and training data, toward the gate in section 7. Each iteration: state one hypothesis, make one change, run train and eval on `dev`, log a row, keep the change or revert it. Stop on **DONE** (gate passed), **PLATEAU** (5 iterations without at least a 1 point gain), **BUDGET** (a time or iteration cap set in advance), or **BLOCKED** (needs a maintainer decision; written in STATE.md).

## 10. Stop and ask a maintainer when

- A dataset's license is unclear
- Anything needs network access beyond pip, Hugging Face model and dataset downloads, and the gitleaks install
- Anything is irreversible
- A result looks too good, such as near-perfect recall on rungs 4 to 5

## 11. Conventions

- Python 3.10 or later. Type hints, small modules, no notebooks in `src/`.
- Tests stay offline on CPU. Add a regression test for every real failure.
- Commit after every completed checkbox with a clear message.
- Write docs and comments in plain language, with no em-dashes.
