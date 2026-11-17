#!/bin/sh
# Full iteration: token probe on new data (layer 15, Qwen3-1.7B) then stage 2.
# usage: scripts/fulliter.sh NAME "DATA FILES" [train_stage2 flags...]
NAME=$1; DATA=$2; shift 2
ROOT=${CASTELLAN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
export HF_HOME=${HF_HOME:-$ROOT/hf_home}
.venv/bin/castellan -v train --data $DATA --model Qwen/Qwen3-1.7B --revision 70d244cc86ccca08cf5af4e1e306ecf908b1ad5e \
  --layers 15 --cache-dir cache --out bundles/${NAME}_tok > runs/train_${NAME}_tok.log 2>&1 &&
DATA="$DATA" scripts/s2iter.sh $NAME ${NAME}_tok "$@" && grep -q done runs/$NAME.done &&
.venv/bin/python scripts/mine_negatives.py flagrate --bundle bundles/$NAME --pool data_out/mined/pool.jsonl --two-stage \
  --out runs/flagrate_$NAME.json > runs/flagrate_$NAME.log 2>&1 &&
echo done > runs/$NAME.full || echo FAIL > runs/$NAME.full
