# STATE

Last updated: 2026-11-16 (gate decisions)

## Current status

- **Milestones 0 and 1: done.**
- **Milestone 2: done and frozen.** The M2 session wrote it on branch `claude/m2-eval-generators-3b0b0f`, which was fast-forward merged into `main` on 2026-11-16 (tip `b15257a`).
  - **Main freeze: `d4eefdc`** writes `eval_data/FROZEN.sha256`. It covers `dev`, `final`, `hardneg`, `ladder`, `longctx` and `secrets_eval` (jsonl), the `ladder.py` and `secrets_eval.py` generators, and `src/castellan/evaluate.py` (sha256 `1d04ca80...`).
  - **Addendum freeze: `b15257a`** writes `eval_data/FROZEN_ADDENDUM.sha256`. It covers `ladder_neutral.jsonl` and `ladder_neutral.py`, the name-neutral ladder of 60 secret bases plus 60 matched controls at rungs 0, 3, 4 and 5.
  - Both `shasum -a 256 -c` checks were all OK after the merge, and the test suite was green (80 passed).
  - Set details and reporting rules are in `eval_data/README.md`. Report rung 4 per class or weighted by item. Report every `ladder_neutral` number with a bootstrap 95% CI that resamples whole bases.
  - This session (the one that wrote `data/synth.py`) reads only `eval_data/README.md` and the jsonl outputs, never the eval generators.
- **M3 prep done (2026-11-16):**
  - Baselines ran on every frozen set: gitleaks default, gitleaks `--max-decode-depth 0`, and Presidio + UK NINO.
  - The 8-gram leakage check found word overlap 0 everywhere, and Qwen3-token overlap with means of 0.3% to 2.5%.
  - The rung-5 gitleaks hits are confirmed to be the 32 PEM items.
- **Gate check: FAILED** (Qwen3-1.7B, layer 15, bundle `bundles/q17_s0`). All four blocking criteria fail; the pattern-PII line is now non-blocking (see the decisions log). Recall beats both baselines, but the probe floods out-of-distribution text: hardneg doc flag rate 0.956 and char FPR 0.434. Details are in EXPERIMENTS.md, "Gate check: Qwen3-1.7B".
- **Not yet run with the probe:** ladder, ladder_neutral and final (per user instruction).
- **Section 9 loop:** owned by a fresh session (user decision 2026-11-16). This session stays out of `data/synth.py`. Budget not yet set.
- **Lookalike phase done; probe-v1 frozen (2026-11-16 21:30 PST).** negatives-1 merged; split is hash-based, no fix needed; RFC JWT capped at 1% in scripts/build_lookalikes.py. Overlap check against every frozen eval set (counts only, user-allowed): no exact duplicates; overlap only from doc_example and public_key; l12's gains held on the overlap-free dev subset with non-overlapping bootstrap CIs, so l11 onward is verified. Rows: l11 0.800 (JWT flag), l12 0.810, l13 0 (JWT flag), then the user's 4 iterations: l14 value extension 0.796 (not kept; exact R 0.524), l15 2% doc-FPR threshold 0.823 (kept), l16 MLP stage 2 0.822 (not kept), l17 MLP plus 1% doc-FPR 0.851 (kept, final). Budget used: 7 of 7. **Frozen: probe-v1 = l17** (configs/probe-v1.json, git tag probe-v1; weights in bundles/probe-v1, outside git, with SHA-256 in the config). **Gate run (user-authorized, once):** probe-v1 FAILED all four gate lines on dev, secrets_eval and hardneg (disclosed gate miss in EXPERIMENTS.md); final not touched. The lk_n10000 hash_digest overlap with one final doc is logged as an open limitation; the lookalike set stays unchanged; when final is scored, report with and without that doc. **M3 in progress** on this branch: readability.py built; chain in tmux session m3 (q17, then q06, q4, q8), with a rung-0 sanity gate after q17. Resolved (was BLOCKED 2026-11-16): the 1.7B rung-0 readability sanity gate failed (0.711 vs 0.95, refusals 0); misses are PEM keys, DB URLs and long JWTs. Chain stopped; waiting for the user's decision (see EXPERIMENTS.md).

**Dev report, probe-v1 (l17).**

| dev | probe-v1 | l03 (before lookalikes) | Gitleaks defaults | Presidio + UK NINO |
|---|---|---|---|---|
| Secret span P / R | 0.851 / 0.834 | 0.627 / 0.785 | 0.979 / 0.753 | n/a |
| Secret exact-boundary recall | 0.413 | 0.459 | 0.738 | n/a |
| Secret char P / R | 0.946 / 0.419 | 0.697 / 0.672 | 0.998 / 0.898 | n/a |
| Secret doc AUROC / R at 1% FPR | 0.942 / 0.427 | 0.897 / 0.187 | 0.894 / 0.796 | n/a |
| Probe secret recall at Gitleaks doc FPR | 0.300 | 0.069 | 0.796 | n/a |
| Pattern PII span P / R | 0.971 / 0.881 | 0.956 / 0.724 | n/a | 0.480 / 1.000 |
| Dev-negative char FPR (hardneg gate 0.005) | 0.0106 | 0.134 | 0.0005 | 0.024 |
| Dev-negative doc flag rate (gate 0.05) | 0.103 | 0.243 | 0.013 | 0.637 |
| Real-code holdout flag rate | 0.000 | 0.002 | not measured | not measured |
| ms per 1k tokens (MPS) | 267 | 266 | n/a | n/a |

- The scoring path is verified on held-out synth: secret P 0.787, R 0.994, doc AUROC 0.998. The gate failure is distribution shift, not a bug.

## Loop-1 session (branch `loop-1`, separate worktree), started 2026-11-16

Owns the section 9 loop (user decision 2026-11-16). It reads docs/DESIGN.md, STATE.md and EXPERIMENTS.md aggregate rows, and never opens anything under `eval_data/`. The only eval it runs is `scripts/dev_eval.py` on `eval_data/dev.jsonl`.

**Plan (user instruction).** Three experiments first, in order: (1) real benign negatives mined from a permissive code corpus, up to 3 rounds; (2) a span-level second stage on pooled activations; (3) Qwen3-4B with the best configuration. Then the section 9 loop.

**Budget (user, 2026-11-16).** The loop stops at 15 iterations or 12 hours, whichever comes first. The three experiments above do not count toward it, and the 12 hour clock starts at the first loop iteration.

**Pre-registered loop score (written before experiment 1, 2026-11-16).**
- Score = dev secret span precision (category level), counted only when dev secret span recall is above gitleaks' dev span recall (0.753). Otherwise the score is 0.
- 1 point = +0.01 on this score. An iteration is kept if the score improves by at least 1 point; ties go to the simpler configuration.
- Always reported alongside: dev secret span recall, exact recall, char P and R, doc AUROC and doc R@1%FPR, the probe's secret doc recall at gitleaks' dev FPR, and the dev-negative block (dev docs with no gold spans, labels ignored: char FPR and doc flag rate). The dev-negative block is a proxy for the hardneg gate lines, never a substitute for them.
- **DONE on dev** means: secret span P >= 0.979 (gitleaks) with span R > 0.753, plus dev-negative char FPR <= 0.005 and doc flag rate <= 0.05. Then stop and report; the user decides whether to run the gate sets.

**Environment note.** This worktree's `.venv` is Python 3.14.6 with numpy 2.4.6 (the gate run used 3.12.13 and numpy 2.5.5). torch 2.15.0, transformers 5.23.0, scikit-learn 1.9.1, presidio and `en_core_web_lg` all load. `castellan synth --n 12000 --seed 0` reproduces data hash `0cd6f64f` exactly. The `cache` symlink into the main repo was removed; this worktree has its own `cache/`, `bundles/`, `runs/` and `data_out/`.

**Real-corpus rules (user, 2026-11-16).** Corpus: codeparrot/github-code-clean, 2 shards, files whose per-file license is MIT, Apache-2.0, BSD-2/3, ISC, CC0 or Unlicense only, plus YAML/JSON/TOML/INI/.cfg files from permissively licensed packages in the venv. Stored under `$CASTELLAN_ROOT/corpora/` and `data_out/`, never in git. Before a chunk becomes a training negative it is dropped if (a) gitleaks flags its file, (b) a credential-like key name sits next to a non-trivial value, (c) it holds URL userinfo, (d) it holds a JWT, PEM private block or known vendor prefix, or (e) a high-entropy value of 20+ chars appears with no hash/ID/digest/checksum context. When in doubt, drop. Only drop-reason counts are logged. If more than 90% of candidates survive the filter, stop and tell the user.

**Loop progress.** STOPPED on PLATEAU at 2026-11-16 13:02 PST after 8 of 15 iterations (about 45 minutes of the 12 hour cap). Iterations 4 to 8 each gained less than 1 point over the best. Best kept configuration: **l03** = Qwen3-1.7B layer 15 token probe trained on synth + r1 + r2 (bundle e1r2, data hash c712de6b), plus stage 2 (cross-fitted proposals at hi 0.2, span mean and max plus 64-token left and right context means, logistic C 0.01, threshold for 0.90 val candidate recall). Loop score 0.627.

**Stop report (dev only; gate sets not run).**

| dev | l03 (best) | Gitleaks defaults | Presidio + UK NINO |
|---|---|---|---|
| Secret span precision | 0.627 | 0.979 | n/a |
| Secret span recall | 0.785 | 0.753 | n/a |
| Secret exact-boundary recall | 0.459 | 0.738 | n/a |
| Secret char P / R | 0.697 / 0.672 | 0.998 / 0.898 | n/a |
| Secret doc AUROC / R at 1% FPR | 0.897 / 0.187 | 0.894 / 0.796 | n/a |
| Probe secret recall at Gitleaks doc FPR (0.0067) | 0.069 | 0.796 | n/a |
| Pattern PII span P / R | 0.956 / 0.724 | n/a | 0.480 / 1.000 |
| Dev negatives char FPR (gate on hardneg: 0.005 or less) | 0.134 | 0.0005 | 0.024 |
| Dev negatives doc flag rate (gate: 0.05 or less) | 0.243 | 0.013 | 0.637 |
| Real-code holdout flag rate (1,000 held-out github-code-clean chunks, training side) | 0.002 (token probe alone: 0.122; gate bundle: 0.998) | not measured | not measured |
| Latency, ms per 1k tokens (MPS) | 266 | n/a | n/a |

Reading: the probe beats Gitleaks on secret span recall (0.785 vs 0.753) but is far below it on precision (0.627 vs 0.979), so the secrets gate line (beat recall at equal or better precision) is not met. The hardneg-style lines are also far off on dev negatives (char FPR 27x the limit, flag rate 5x). Pattern PII precision is twice Presidio's, but recall is lower (non-blocking line). Running the gate sets now would almost certainly fail; that decision is the user's.

Largest gains this session: mined real-code negatives (+20 points over two rounds), the stage-2 span classifier (+9.4), its 0.90 operating point (+6.7) and 64-token context (+2.9). What did not help: Qwen3-4B (tested only at the e2s2 stage-2 settings, not at l03's), a third mining round (as stage-2 data the test was confounded because the proposer changed too), class one-hot, per-class thresholds (recall fell below the floor, but dev-negative char FPR dropped to 0.038), tighter proposals, a second pooled layer. Stage-2 val AUROC sits near 0.985 in every run while dev precision is near 0.6, so the remaining gap is a train/dev distribution shift, not classifier capacity. The real-code flag rate is near zero while dev negatives still flag at 0.243, so the remaining false positives come from lookalike text unlike the mined code, not from ordinary code. Note: l04 (context 128) scored 0.631, nominally above l03, but below the pre-registered 1-point keep bar, so l03 remains the kept best. Candidate next steps for the user to choose: mine negatives with the two-stage detector (stage-2 false positives on real code), more varied synthetic carriers, or an attention probe over the window (M4).

## Machine (confirmed 2026-11-15)

| Item | Value |
| --- | --- |
| Hardware | Apple Silicon, 48 GB unified memory |
| OS | macOS 26.5.2 (25F84) |
| Python (venv) | 3.12.13, created with `uv venv --python 3.12` (system default is 3.14) |
| torch | 2.15.0. MPS is available, and a bf16 matmul smoke test gave no NaN. |
| transformers | 5.23.0 (Qwen3 supported) |
| tokenizers | 0.23.3 |
| scikit-learn | 1.9.1 |
| numpy | 2.5.5 |
| presidio-analyzer | 2.2.362 |
| spacy | 3.8.18. `en_core_web_lg` is not installed yet. |

## Pending asks for the user

Loop-1 stopped on PLATEAU on 2026-11-16. Decide whether to run the gate sets (hardneg, secrets_eval) on l03, and which next step to take. Earlier asks were resolved on 2026-11-15 (see the decisions log).

## Decisions log

| Date | Decision | Why |
| --- | --- | --- |
| 2026-11-15 | Main branch is `main`. The repo-local git identity is set. | The harness expects `main`. No global git identity existed. |
| 2026-11-15 | Python 3.12 venv. | torch, spaCy and presidio wheels lag new Python releases, and the system default is 3.14. |
| 2026-11-15 | Build backend is hatchling with a src layout. Core deps are numpy and scikit-learn. Extras are torch, presidio, server, dev and all. | Follows the brief. A `dev` extra holds pytest. |
| 2026-11-15 | Offsets are Python code-point indices, and text is never normalized on read or write (`ensure_ascii=False`, no NFC or NFKC). | Rung 3 uses zero-width characters and homoglyphs. Normalizing would shift offsets. |
| 2026-11-15 | `Example` and `Span` reprs, and `SchemaError` messages, never include text or values. | pytest failure output must not print sensitive values (section 2). |
| 2026-11-15 | The tiny fixture plants its signal by adding a direction to the **embedding rows** of secret-only tokens (alpha 1.0, 4 layers, hidden size 64). | The signal flows through the real residual stream and the real hooks, with no label leakage at inference. Separator tokens inside spans stay unplanted to exercise span bridging. |
| 2026-11-15 | The fixture tokenizer mirrors Qwen3: byte-level BPE with `trim_offsets=False`. | Real Qwen3 offsets include a token's leading space. **spans.py must trim leading whitespace from token offsets** before mapping to char spans. A fixture test caught this. |
| 2026-11-15 | Layer k means the output of `model.model.layers[k]`, which equals `hidden_states[k+1]` for every layer **except the deepest**. | In transformers 5.x the last `hidden_states` entry has the final RMSNorm applied. Hooks are the source of truth for the raw residual stream. Tests pin both cases. |
| 2026-11-15 | Set `HF_HOME` to a directory on the large data volume before downloading Qwen3 models in M3 (the scripts now default to `$CASTELLAN_ROOT/hf_home`). | Qwen3-8B plus the activation caches are large, and the data volume has the space. |
| 2026-11-15 | Thresholds: `hi` (span start) is the (1 - target) quantile of each gold span's **peak** score on validation, capped at 0.5. `lo` (continue) is the score that 0.5% of unlabeled validation tokens exceed. Both have a 0.05 floor. | Token-recall thresholds flooded benign text. Separator tokens inside values are labeled positive but carry little signal, which dragged the threshold down to 0.10. The planted-signal test caught it. Bridging handles the low-scoring separators. |
| 2026-11-15 | Activations are cached as raw bf16 bits for every model dtype. Per token, `extract` keeps the activation from the first window that contains it. Scoring takes the max over overlapping windows. | Halves disk use against fp32, and converts exactly. The first window gives the token the most left context. |
| 2026-11-15 | Probe heads are numpy mini-batch Adam on weighted BCE, with the positive weight capped at 50, then Platt scaling per class on validation. | Keeps `probe.py` torch-free. Platt scaling undoes the bias from the positive weighting. |
| 2026-11-15 | Training synth holds **plain** secrets only. There are no split, encoded, homoglyph or spelled-out variants. | The ladder must measure generalization, not something we taught. |
| 2026-11-15 | Evaluation matches spans at the **category** level (secret, pattern PII, contextual PII) for the headline numbers, plus a strict class-level breakdown. | Gitleaks and Presidio label schemes do not map one to one onto ours. For example, gitleaks' generic rule catches cloud keys. |
| 2026-11-15 | The gitleaks wrapper locates the reported secret in the text, and falls back to byte columns only for decoded findings. | Gitleaks columns are byte based and 1-based, and they drift by one per line after the first. |
| 2026-11-15 | `HF_HOME` on the data volume, with Qwen3-0.6B pinned at `c1899de289a04d12100db370d81485cdf75e47ca`. | Pin every model revision in bundles and EXPERIMENTS rows. |
| 2026-11-15 | **ai4privacy is skipped for training.** PII stays synthetic. | User decision. Secrets are the research question, `license: other` is unclear, and the 500k version adds Llama terms. Weights trained on synthetic data only are easier to release. A later eval-only check may use a sample, once the authors are contacted. |
| 2026-11-15 | `en_core_web_lg` 3.8.0 is installed from spaCy's GitHub release. | User approved. The Presidio + UK NINO baseline needs it. |
| 2026-11-15 | **Hardneg gate locked:** with labels ignored, `any_label.char_fpr` <= 0.005 and `any_label.neg_doc_flag_rate` <= 0.05. | User decision, made before the M2 freeze. Char precision is undefined with no gold spans. Planting secrets would change what the set means, and pooling it with dev would blur the result. The document number compares directly with gitleaks. |
| 2026-11-15 | `evaluate.py` adds `compare_operating_points`: the probe's recall at a non-scoring baseline's document FPR, and its FPR at the baseline's recall, per category and per rung. `castellan eval` writes this under `comparisons`. | User request, made before the M2 freeze. A non-scoring baseline has one operating point, so comparing at 1% FPR alone is unfair in either direction. |
| 2026-11-15 | Presidio adds `UkNinoRecognizer` (UK_NINO, mapped to GOV_ID). Span edges are trimmed of whitespace. The recognizer list is stored in every eval report and in EXPERIMENTS.md. | User request. Trimming changes no Presidio decision. |
| 2026-11-15 | tmux 3.8 is installed. | Installed by the user. |
| 2026-11-15 | Tests set `HF_HUB_OFFLINE=1` and build the tiny model in a session-scoped temp dir. | Tests must be offline and run on CPU. |
| 2026-11-16 | **Pattern-PII gate line is non-blocking** (information only). `scripts/gate_eval.py` writes it under `info`, not `gate`, and it never affects `gate_passed`. The probe's FPR at the baseline's recall is reported as information only. | User decision. The baseline's pattern-PII recall is 1.0 on dev and secrets_eval, so "beat it on recall" cannot be met by any detector. Swapping in a matched-operating-point test after seeing the gate results would be post hoc. |
| 2026-11-16 | The Presidio baseline is named **"Presidio + UK NINO"** in every report and doc. `PresidioDetector.display_name` carries it; the machine key `presidio` stays in file names and CLI flags. | User decision. UkNinoRecognizer is not in Presidio's default registry, so "Presidio defaults" was inaccurate. |
| 2026-11-16 | The section 9 loop runs in a **fresh session**. This session does not edit `data/synth.py`. | User decision. This session wrote synth.py and has read `eval_data/README.md` and the hardneg false-positive breakdowns, so its changes could tune against eval sets (section 8). |

- **M3 (d) done (2026-11-17).** User fixed the readability prompt (v2, disclosed), conditioned the primary analysis on bases read at rung 0, kept exact match. Chain q17, q06, q4, q8 finished (exit 0). Rung-0 readability: 0.6B 0.489, 1.7B 0.856, 4B 0.831, 8B 0.863; zero refusals and think blocks; every size flagged (below 0.95). Per-size probes (recipe unchanged): 0.6B L15 dev P 0.861 R 0.817; 4B L20 P 0.867 R 0.837; 8B L25 P 0.801 R 0.845. Results and tables: EXPERIMENTS.md 'M3 results: rung x model size'. Not done yet: the plot, the probe architecture comparison, the blog-style summary section. Waiting for the user.
- **M3 follow-up analysis done (2026-11-17).** Matched 5% control FPR and AUROC per rung, conditional test (flag | can read), union probe OR Gitleaks, rung-3 confound check by variant; 8B DB URL partial answers logged as model behavior; headline plot docs/m3_headline.png; plain-language Results summary written. Probe architecture comparison deferred to M4. No retraining or threshold changes. Waiting for the user.
- **M3 DONE (2026-11-17).** Final scored once with probe-v1 at 1fcba0d: secret span P 0.865, R 0.859 (Gitleaks 0.979 / 0.745), doc R@1%FPR 0.373, negatives char FPR 0.0057, flag rate 0.080; identical without the lookalike-overlap doc (a negative, not flagged). Recorded as the held-out result in EXPERIMENTS.md. No changes after the final run. Probe architecture comparison deferred to M4. loop-1 is ready for the user's review and merge (the user merges, not Claude).
