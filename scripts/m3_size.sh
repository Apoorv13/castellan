#!/bin/sh
# M3: probe-v1 recipe, unchanged, on one model size, then ladder eval and readability.
# usage: scripts/m3_size.sh NAME MODEL REVISION
#   NAME q06 | q4 | q8. For q17 (probe-v1 itself) use: scripts/m3_size.sh q17 - - (skips training).
NAME=$1; MODEL=$2; REV=$3
ROOT=${CASTELLAN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
export HF_HOME=${HF_HOME:-$ROOT/hf_home}
DATA="data_out/train_s0_n12000.jsonl data_out/mined/r1.jsonl data_out/mined/r2.jsonl data_out/lk_n5000_nodoc.jsonl"
mkdir -p runs/m3/readability
set -e
if [ "$NAME" = q17 ]; then
  B=bundles/probe-v1
  MODEL=$(python3 -c "import json;print(json.load(open('$B/bundle.json'))['model_id'])")
  REV=$(python3 -c "import json;print(json.load(open('$B/bundle.json'))['revision'])")
else
  .venv/bin/castellan -v train --data $DATA --model "$MODEL" --revision "$REV" --cache-dir cache \
    --out bundles/m3_${NAME}_tok > runs/train_m3_${NAME}_tok.log 2>&1
  DATA="$DATA" scripts/s2iter.sh m3_$NAME m3_${NAME}_tok --target-recall 0.90 --ctx 64 --target-doc-fpr 0.01 --mlp 256
  grep -q done runs/m3_$NAME.done
  B=bundles/m3_$NAME
fi
if [ ! -f runs/m3/$NAME/ladder__probe.json ]; then
  .venv/bin/python scripts/ladder_eval.py --bundle $B --detector castellan.stage2:detector_factory \
    --out runs/m3/$NAME > runs/m3/ladder_$NAME.log 2>&1
fi
for S in ladder_neutral ladder; do
  .venv/bin/castellan -v readability --data eval_data/$S.jsonl --model "$MODEL" --revision "$REV" \
    --out runs/m3/readability/${NAME}__$S.jsonl > runs/m3/readability_${NAME}_$S.log 2>&1
done
if [ "$NAME" = q17 ]; then
  # Continue rule (user, 2026-11-16): rung-0 can_read >= 0.80 with zero refusals, else stop.
  .venv/bin/python scripts/readability_summary.py q17 --gate || { echo STOP > runs/m3/STOP; exit 1; }
else
  .venv/bin/python scripts/readability_summary.py $NAME
fi
echo done > runs/m3/$NAME.done
