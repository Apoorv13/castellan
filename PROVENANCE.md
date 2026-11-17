# Provenance

This public repository starts from a single commit. The full development history (about 100 commits from 2026-11-15 to 2026-11-17, covering every experiment logged in `EXPERIMENTS.md`) is kept privately. **It is available to reviewers on request.** The commit hashes below, and every short commit SHA cited in `EXPERIMENTS.md` and `STATE.md` (for example `2a794ed` or `1fcba0d`), refer to that private history.

## When the eval sets were frozen

The eval sets were written and frozen before any probe was evaluated on them (`docs/DESIGN.md`, sections 5 and 8).

| Freeze | Commit (private history) | Date | Manifest |
| --- | --- | --- | --- |
| Main freeze | `d4eefdc` (`d4eefdcf332398938219ad0c9ad75fd26ff62cb5`) | 2026-11-16 00:14:27 -08:00 | `eval_data/FROZEN.sha256` |
| Addendum: `ladder_neutral` | `b15257a` (`b15257a0e8775c8492bb699024533ce5f73df982`) | 2026-11-16 00:20:55 -08:00 | `eval_data/FROZEN_ADDENDUM.sha256` |

### `eval_data/FROZEN.sha256`

```
8e7a67e6e2e27a7d42845f73e5f7dcee90114825230e08af643f5111f4e78856  eval_data/dev.jsonl
f32c0c8880578770fa5e5ef39f80f024d1d07d4f46ced13834d8c3c375d1ba40  eval_data/final.jsonl
01cd0b964ef7a5775d08def351b265e62fb07ce3247c417d15216ebe25e2efbc  eval_data/hardneg.jsonl
0a7064d25c303fbe15effc752acc057965be8d36423ca09a6ec207a30aeef4b8  eval_data/ladder.jsonl
afb5f71d4fe0f49c3d4ebd65b6121316690b8e795c7dfd45bbc7451fc4f947de  eval_data/longctx.jsonl
bb989043aff48483eaa6ee4b25741bf74fead812f44af919ac5234a0af4f4153  eval_data/secrets_eval.jsonl
04e1d361aa0765730fecb34fa6bc8966e07978ef4a989993cbb4f6f1cd726352  eval_data/ladder.py
ba97bdbfeceb462b2dfc0f98a32b76d9504fee4e94a53e74f00e7b88258af659  eval_data/secrets_eval.py
1d04ca80b31cac2b35e810154b8426cb7a16e13e962df9bdec10c4420cd83f9d  src/castellan/evaluate.py
```

### `eval_data/FROZEN_ADDENDUM.sha256`

```
b05ee66b2b43bf54a1084881c9a5d028a0f1d8d82c64150d32247e3e8d5967fb  eval_data/ladder_neutral.jsonl
9bd8862d17639749fc0bf5f8107a0ffcb9759a68f8ca63961bda96cffeb167a9  eval_data/ladder_neutral.py
```

The generators and `src/castellan/evaluate.py` in this repository are byte-identical to the frozen versions. `python scripts/eval_data.py` regenerates every JSONL file and checks all eleven hashes. It exits non-zero on any mismatch. Regeneration is deterministic, and was checked on Python 3.12.13 and 3.14.6.

## Release assets

Both archives are built by `scripts/make_release_assets.py` with fixed mtimes and owners and sorted members, so a rebuild in the same environment gives the same hashes. The gzip bytes can differ with another zlib version; the files inside do not, and the eval files are checked against the freeze manifests either way.

```
bf7420b10eef663caa6ec497d9a7a4aefe64bac39b1800f3857f23c421e4c3fd  castellan-eval-data-v1.tar.gz
31a3d324d04d04f321d04463ffbba2e5670a9b9b81eab7a9d499bbc086af12a3  castellan-m3-runs-v1.tar.gz
```

## What was held out, and what no longer is

- `final.jsonl` was scored once, on 2026-11-17, after all choices were made (`EXPERIMENTS.md`, "Held-out result"). **Now that it is published, it is no longer held out.** Future work should treat it as a public test set and build a new held-out set for its own claims.
- The ladder sets were never used to train, tune, calibrate or choose anything.

## Probe weights

The probe-v1 weights are not in this repository. `configs/probe-v1.json` pins the model revision, the training-data hashes, the full recipe and the SHA-256 of every bundle file.
