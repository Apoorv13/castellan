#!/bin/sh
# Reproduce the headline tables (README "Results" and EXPERIMENTS.md "M3 results").
#
# usage: scripts/reproduce.sh [STAGE...]
#   eval       any env  Regenerate the frozen eval sets and verify their SHA-256s.
#   tables     py314    Rebuild the M3 tables, analysis and plot from run outputs in runs/,
#                       then compare them byte for byte with runs/reference/. This is the
#                       default. With no local runs, unpack the release asset first:
#                         tar -xzf castellan-m3-runs-v1.tar.gz
#   baselines  py312    Re-run Gitleaks 8.30.1 and Presidio + UK NINO on every frozen set.
#   probe      py314    Retrain probe-v1 (Qwen3-1.7B, layer 15, two-stage) from its recipe.
#                       Needs the mined real-code negatives (see README); MPS, hours.
#   m3         py314    Train the 0.6B, 4B and 8B probes, run the ladder evals and the
#                       readability scorer for all four sizes. MPS, many hours, about 40 GB of
#                       model downloads.
#   all                 eval baselines probe m3 tables
#
# Each stage names the lockfile of the environment that produced the published
# numbers. Results came from two environments:
#   requirements-lock-py312.txt  baselines, and the first gate check (bundle q17_s0)
#   requirements-lock-py314.txt  probe-v1 training and gate run, every M3 probe and readability
#                                run, the tables and plot, the final scoring
# The script checks the running Python and packages against that lockfile and warns
# on a mismatch (set STRICT=1 to stop instead). PYTHON picks the interpreter (default python3).
#
# Run from anywhere; the script works in the repo root (runs/, bundles/, cache/ and data_out/
# live there, all gitignored). CASTELLAN_ROOT (default: the repo root) holds the large local
# data: corpora/ and hf_home/. HF_HOME defaults to $CASTELLAN_ROOT/hf_home.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export CASTELLAN_ROOT=${CASTELLAN_ROOT:-$ROOT}
export HF_HOME=${HF_HOME:-$CASTELLAN_ROOT/hf_home}
PY=${PYTHON:-python3}
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
SIZES="q06 q17 q4 q8"

need_env() {  # need_env py312|py314 [--gitleaks]
  echo "== stage needs $1 (requirements-lock-$1.txt)"
  if [ "${STRICT:-0}" = 1 ]; then "$PY" "$ROOT/scripts/check_env.py" "$@" --strict
  else "$PY" "$ROOT/scripts/check_env.py" "$@"; fi
}

check_sha() {  # check_sha FILE SHA256
  got=$(shasum -a 256 "$1" | cut -d' ' -f1)
  if [ "$got" = "$2" ]; then echo "OK        $1"; else echo "MISMATCH  $1"; return 1; fi
}

probe_cfg() {  # probe_cfg KEY.PATH -> value from configs/probe-v1.json
  "$PY" -c "import json,sys;d=json.load(open('$ROOT/configs/probe-v1.json'))
for k in sys.argv[1].split('|'): d=d[k]
print(d)" "$1"
}

legacy_layout() {  # the per-run helper scripts call ./.venv/bin/python and ./.venv/bin/castellan
  if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "The $1 stage calls helper scripts that expect the venv at ./.venv (built from requirements-lock-py314.txt)."
    return 1
  fi
}

stage_eval() {
  echo "== eval: regenerate and verify"
  "$PY" "$ROOT/scripts/eval_data.py"
}

stage_baselines() {
  need_env py312 --gitleaks
  "$PY" "$ROOT/scripts/run_baselines.py" --out runs/baselines --eval-dir "$ROOT/eval_data"
}

stage_probe() {
  need_env py314
  legacy_layout probe
  mkdir -p data_out runs bundles
  [ -f data_out/train_s0_n12000.jsonl ] || "$PY" -m castellan.cli synth --n 12000 --seed 0 --out data_out/train_s0_n12000.jsonl
  [ -f data_out/lk_n5000_nodoc.jsonl ] || "$PY" "$ROOT/scripts/build_lookalikes.py" --n 5000 --drop-category doc_example --out data_out/lk_n5000_nodoc.jsonl
  ok=0
  for f in data_out/train_s0_n12000.jsonl data_out/mined/r1.jsonl data_out/mined/r2.jsonl data_out/lk_n5000_nodoc.jsonl; do
    if [ ! -f "$f" ]; then echo "MISSING   $f"; ok=1; continue; fi
    check_sha "$f" "$(probe_cfg "training_files_sha256|$f")" || ok=1
  done
  if [ $ok != 0 ]; then
    echo "Training inputs are missing or differ from configs/probe-v1.json."
    echo "data_out/mined/r1.jsonl and r2.jsonl are real-code chunks mined from codeparrot/github-code-clean"
    echo "(permissive per-file licenses only) and are not redistributed; see README, 'Reproducing probe-v1'."
    return 1
  fi
  # Recipe from configs/probe-v1.json: the token probe l12_tok (the training step of
  # scripts/fulliter.sh: Qwen3-1.7B, layer 15), then stage 2 as l17 (scripts/s2iter.sh).
  DATA="data_out/train_s0_n12000.jsonl data_out/mined/r1.jsonl data_out/mined/r2.jsonl data_out/lk_n5000_nodoc.jsonl"
  "$PY" -m castellan.cli -v train --data $DATA --model "$(probe_cfg model_id)" --revision "$(probe_cfg revision)" \
    --layers "$(probe_cfg layer)" --cache-dir cache --out bundles/l12_tok > runs/train_l12_tok.log 2>&1
  DATA="$DATA" "$ROOT/scripts/s2iter.sh" l17 l12_tok --target-recall 0.90 --ctx 64 --target-doc-fpr 0.01 --mlp 256
  grep -q done runs/l17.done
  rm -rf bundles/probe-v1 && cp -R bundles/l17 bundles/probe-v1
  echo "== probe-v1 bundle vs configs/probe-v1.json (bf16 on MPS; a mismatch here means a numeric difference, not a failure of the recipe)"
  for f in bundle.json probe.npz stage2/stage2.json stage2/stage2.npz; do
    check_sha "bundles/probe-v1/$f" "$(probe_cfg "bundle_files_sha256|$f")" || true
  done
}

stage_m3() {
  need_env py314
  legacy_layout m3
  "$ROOT/scripts/m3_size.sh" q17 - -
  "$ROOT/scripts/m3_size.sh" q06 Qwen/Qwen3-0.6B c1899de289a04d12100db370d81485cdf75e47ca
  "$ROOT/scripts/m3_size.sh" q4 Qwen/Qwen3-4B 1cfa9a7208912126459214e8b04321603b3df60c
  "$ROOT/scripts/m3_size.sh" q8 Qwen/Qwen3-8B b968826d9c46dd6066d109eabc6255188de91218
}

stage_tables() {
  need_env py314
  for n in $SIZES; do
    [ -d "runs/m3/$n/preds" ] || { echo "runs/m3/$n is missing. Unpack castellan-m3-runs-v1.tar.gz in the repo root or run the m3 stage."; return 1; }
  done
  mkdir -p docs
  "$PY" "$ROOT/scripts/m3_report.py" $SIZES > /dev/null
  "$PY" "$ROOT/scripts/m3_analysis.py" $SIZES > /dev/null
  "$PY" "$ROOT/scripts/m3_tables.py" $SIZES > runs/m3/tables.md
  "$PY" "$ROOT/scripts/m3_analysis_tables.py" $SIZES > runs/m3/analysis_tables.md
  "$PY" "$ROOT/scripts/m3_plot.py" $SIZES --out runs/m3/m3_headline.png
  if [ -d runs/reference ]; then
    echo "== compare with runs/reference"
    ok=0
    for f in report.json analysis.json tables.md analysis_tables.md; do
      if cmp -s "runs/m3/$f" "runs/reference/$f"; then echo "SAME      $f"; else echo "DIFFERENT $f"; ok=1; fi
    done
    if cmp -s runs/m3/m3_headline.png "$ROOT/docs/m3_headline.png"; then echo "SAME      m3_headline.png (docs/)"; else echo "DIFFERENT m3_headline.png (docs/; may differ with another matplotlib)"; fi
    return $ok
  fi
  echo "tables written to runs/m3/ (no runs/reference to compare against)"
}

[ $# -eq 0 ] && set -- tables
[ "$1" = all ] && set -- eval baselines probe m3 tables
for s in "$@"; do
  case $s in
    eval|tables|baselines|probe|m3) "stage_$s" ;;
    *) echo "unknown stage: $s"; exit 2 ;;
  esac
done
