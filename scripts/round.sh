#!/bin/sh
# One train -> dev eval -> real-code flag rate round.
# usage: scripts/round.sh NAME MODEL REVISION LAYER DATA...
set -e
NAME=$1; MODEL=$2; REV=$3; LAYER=$4; shift 4
ROOT=${CASTELLAN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
export HF_HOME=${HF_HOME:-$ROOT/hf_home}
.venv/bin/castellan -v train --data "$@" --model "$MODEL" --revision "$REV" --layers "$LAYER" \
  --cache-dir cache --out bundles/$NAME > runs/train_$NAME.log 2>&1
.venv/bin/python scripts/dev_eval.py --bundle bundles/$NAME --out runs/dev_$NAME.json > runs/dev_$NAME.log 2>&1
.venv/bin/python scripts/mine_negatives.py flagrate --bundle bundles/$NAME --pool data_out/mined/pool.jsonl \
  --out runs/flagrate_$NAME.json > runs/flagrate_$NAME.log 2>&1
echo done > runs/$NAME.done
