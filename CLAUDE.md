# CLAUDE.md

Instructions for coding agents working in this repo.

- Read `docs/DESIGN.md` first. It holds the design, the ground rules (section 2), the metrics and gate (section 7) and the integrity rules (section 8). Section numbers cited in `STATE.md` and `EXPERIMENTS.md` refer to it.
- The frozen files (`eval_data/ladder.py`, `eval_data/secrets_eval.py`, `src/castellan/evaluate.py`) still cite "CLAUDE.md section N". They mean `docs/DESIGN.md` section N. The text can't be changed without breaking the frozen hashes.
- Track progress in `STATE.md` and log results in `EXPERIMENTS.md`.
- Never edit a file listed in `eval_data/FROZEN.sha256` or `eval_data/FROZEN_ADDENDUM.sha256`. Regenerate the eval sets with `python scripts/eval_data.py`.
- Never print or log matched sensitive values, even synthetic ones.
- Tests run offline on CPU: `pytest`.
- Write docs and comments in plain language, with no em-dashes.
