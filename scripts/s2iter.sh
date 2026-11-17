#!/bin/sh
# One stage-2 loop iteration: copy a token-probe bundle, retrain stage 2 with
# the given flags, dev eval, write runs/NAME.done.
# usage: scripts/s2iter.sh NAME BASE_BUNDLE [train_stage2 flags...]
NAME=$1; BASE=$2; shift 2
ROOT=${CASTELLAN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
export HF_HOME=${HF_HOME:-$ROOT/hf_home}
DATA=${DATA:-"data_out/train_s0_n12000.jsonl data_out/mined/r1.jsonl data_out/mined/r2.jsonl"}
rm -rf bundles/$NAME && mkdir -p bundles/$NAME && cp bundles/$BASE/bundle.json bundles/$BASE/probe.npz bundles/$NAME/ &&
.venv/bin/python scripts/train_stage2.py --bundle bundles/$NAME --data $DATA "$@" > runs/train_${NAME}_s2.log 2>&1 &&
.venv/bin/python scripts/dev_eval.py --bundle bundles/$NAME --detector castellan.stage2:detector_factory \
  --out runs/dev_$NAME.json > runs/dev_$NAME.log 2>&1 &&
.venv/bin/python scripts/fp_diag.py runs/dev_$NAME.preds.json > runs/fpdiag_$NAME.json &&
echo done > runs/$NAME.done || echo FAIL > runs/$NAME.done
