"""Append one Runs row to EXPERIMENTS.md from a dev_eval report and a flag-rate report.

    python scripts/log_row.py NAME "train data description" "notes"
"""

import json
import sys
from datetime import date
from pathlib import Path

name, data_desc, notes = sys.argv[1], sys.argv[2], sys.argv[3]
r = json.loads(Path(f"runs/dev_{name}.json").read_text())
bm = json.loads(Path(f"bundles/{name}/bundle.json").read_text())["meta"]
fr = Path(f"runs/flagrate_{name}.json")
fr = json.loads(fr.read_text()) if fr.exists() else None
p, n, mt = r["secret"]["probe"], r["dev_negatives_any_label"], r["matched_vs_gitleaks_secret"]
pii = r["pattern_pii"]["probe"]


def f(x):
    return "n/a" if x is None else f"{x:.3f}"


key = (f"**Loop score {f(r['loop_score'])}**. dev secrets: span P {f(p['span_precision'])}, R {f(p['span_recall'])}, "
       f"exact R {f(p['exact_recall'])}, char P {f(p['char_precision'])}, char R {f(p['char_recall'])}, "
       f"doc AUROC {f(p['doc_auroc'])}, doc R@1%FPR {f(p['doc_recall_at_0.01_fpr'])}, R at gitleaks FPR "
       f"{f(mt['probe_recall_at_baseline_fpr'])}. dev negatives: char FPR {f(n['char_fpr'])}, doc flag rate "
       f"{f(n['neg_doc_flag_rate'])}. Pattern PII: span P {f(pii['span_precision'])}, R {f(pii['span_recall'])}.")
if fr:
    key += f" Real-code holdout flag rate {fr['flag_rate']:.3f} ({fr['spans_per_chunk']:.2f} spans per chunk)."
key += f" {r['timing']['ms_per_1k_tokens']:.0f} ms per 1k tokens."
row = (f"| {date.today()} | {bm.get('git_sha', '')[:7]} | {r['model_id']} | {r['revision']} | {r['dtype']} | {r['layer']} | "
       f"{bm['data_hash'][:8]} ({data_desc}) | dev ({r['dev_hash'][:8]}) | {bm['seed']} | {key} | {name}: {notes} |\n")
s = Path("EXPERIMENTS.md").read_text()
i = s.index("\n## Gate check: Qwen3-1.7B")
s = s[:i].rstrip("\n") + "\n" + row + s[i:]
Path("EXPERIMENTS.md").write_text(s)
print(row)
