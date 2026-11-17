"""Train the span-level second stage on top of a token-probe bundle.

    python scripts/train_stage2.py --bundle bundles/e1r1 --data data_out/train_s0_n12000.jsonl data_out/mined/r1.jsonl

The data must be exactly the bundle's training data (same hash), so its
activation cache is reused. Training data only: synth plus mined real
negatives. Never eval_data/.

1. Same doc-level train/val split as train.py.
2. Cross-fit: split the train-side docs into halves A and B, fit a token probe
   on each half, Platt-calibrate each on the val tokens, and let each propose
   candidates on the other half. Val docs get proposals from the bundle's own
   probe, which never saw them.
3. Candidate label = 1 when at least half of its tokens carry any gold label.
4. Logistic regression on pooled features (see castellan.stage2). C is chosen
   by val AUROC, then Platt scaling and the threshold come from val.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np

from castellan.bundle import load_bundle
from castellan.cache import LABEL_TO_ID, ActivationCache, dataset_hash
from castellan.data.schema import read_jsonl
from castellan.extractor import trim_offsets
from castellan.probe import collect_logits, fit_platt, sigmoid
from castellan.stage2 import Stage2, all_features, propose
from castellan.train import TrainConfig, _fit_probe, split_docs

log = logging.getLogger("train_stage2")


def half(ex_id: str, seed: int) -> bool:
    h = hashlib.sha256(f"half:{seed}:{ex_id}".encode()).digest()
    return h[0] < 128


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--data", nargs="+", required=True)
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--prop-hi", type=float, default=0.2)
    ap.add_argument("--ctx", type=int, default=32)
    ap.add_argument("--target-recall", type=float, default=0.95)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--prop-lo-floor", type=float, default=0.0, help="minimum proposal continuation threshold")
    ap.add_argument("--per-class-thr", action="store_true",
                    help="threshold per class at the target recall on that class's val candidates")
    ap.add_argument("--class-target-recall", type=float, default=None,
                    help="per-class target recall (defaults to --target-recall)")
    ap.add_argument("--target-doc-fpr", type=float, default=None,
                    help="set the threshold so this fraction of negative val docs gets any kept candidate")
    ap.add_argument("--mlp", type=int, default=0, help="hidden units for a one-layer MLP stage 2 (0 = logistic)")
    ap.add_argument("--min-class-pos", type=int, default=50, help="fallback to the global threshold below this")
    ap.add_argument("--layer2", type=int, default=None, help="also pool features from this layer")
    ap.add_argument("--onehot", action="store_true", help="add a one-hot of the proposed class to the features")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    t0 = time.time()

    b = load_bundle(args.bundle, device="mps")
    exs = []
    for p in args.data:
        exs.extend(read_jsonl(p))
    dh = dataset_hash(exs)
    if dh != b.meta.get("data_hash"):
        raise SystemExit("data hash does not match the bundle's training data")
    lc = ActivationCache(args.cache_dir).get(b.model_id, b.revision, b.dtype, b.layer, dh)
    if lc is None:
        raise SystemExit("no activation cache for this bundle and data")
    lc2 = None
    if args.layer2 is not None:
        lc2 = ActivationCache(args.cache_dir).build(b.extractor, exs, [args.layer2], dh)[args.layer2]
        if len(lc2.docs) != len(lc.docs):
            raise SystemExit("layer2 cache does not line up with the bundle cache")

    from transformers import AutoTokenizer

    tokzr = AutoTokenizer.from_pretrained(b.model_id, revision=b.revision)
    labels = b.labels
    class_ids = [LABEL_TO_ID[lab] for lab in labels]
    cfg = TrainConfig(model_id=b.model_id, revision=b.revision, seed=b.meta.get("seed", 0))

    val_docs = split_docs(exs, 0.2, cfg.seed)
    is_a = np.array([half(e.id, args.seed) for e in exs])
    tok_doc = lc.docs
    val_tok = val_docs[tok_doc]
    idx_a = np.flatnonzero(~val_tok & is_a[tok_doc])
    idx_b = np.flatnonzero(~val_tok & ~is_a[tok_doc])
    idx_v = np.flatnonzero(val_tok)
    y_v = (lc.labels[idx_v][:, None] == np.asarray(class_ids)[None, :]).astype(int)

    probes = {}
    for name, idx in (("A", idx_a), ("B", idx_b)):
        pr = _fit_probe(lc, idx, class_ids, labels, cfg, args.epochs)
        z = collect_logits(lc, idx_v, pr)
        for c in range(len(labels)):
            pr.platt_a[c], pr.platt_b[c] = fit_platt(z[:, c], y_v[:, c])
        pr.lo = b.probe.lo.copy()
        probes[name] = pr
        log.info("half probe %s fitted on %d tokens", name, len(idx))

    starts = np.searchsorted(tok_doc, np.arange(len(exs)))
    ends = np.searchsorted(tok_doc, np.arange(len(exs)), side="right")
    prop_hi = np.full(len(labels), args.prop_hi)
    prop_lo = np.maximum(np.minimum(b.probe.lo, prop_hi), args.prop_lo_floor)

    feats = {"train": [], "val": []}
    ys = {"train": [], "val": []}
    cls = {"train": [], "val": []}
    vdoc = []  # doc index per val candidate
    for i, ex in enumerate(exs):
        s, e = starts[i], ends[i]
        if e <= s:
            continue
        if val_docs[i]:
            pr, split = b.probe, "val"
        else:
            pr, split = (probes["B"] if is_a[i] else probes["A"]), "train"
        acts = lc.read(slice(s, e))
        acts2 = None if lc2 is None else lc2.read(slice(s, e))
        enc = tokzr(ex.text, return_offsets_mapping=True, add_special_tokens=False)
        offs = trim_offsets(ex.text, enc["offset_mapping"])
        if len(offs) != e - s:
            raise SystemExit("tokenization does not match the cache")
        sc = pr.proba(acts)
        gold = lc.labels[s:e] != 0
        for c in propose(ex.text, offs, sc, labels, prop_hi, prop_lo, b.max_gap):
            feats[split].append(all_features(acts, acts2, c, args.ctx, labels if args.onehot else None))
            ys[split].append(int(gold[c.tok_start: c.tok_end + 1].mean() >= 0.5))
            cls[split].append(labels.index(c.label))
            if split == "val":
                vdoc.append(i)
        if i % 2000 == 0:
            log.info("doc %d/%d, %d train and %d val candidates", i, len(exs), len(ys["train"]), len(ys["val"]))

    Xt, yt = np.stack(feats["train"]), np.array(ys["train"])
    Xv, yv = np.stack(feats["val"]), np.array(ys["val"])
    log.info("train candidates %d (pos %d), val candidates %d (pos %d)", len(yt), yt.sum(), len(yv), yv.sum())
    mean = Xt.mean(0)
    scale = Xt.std(0) + 1e-6
    Zt, Zv = (Xt - mean) / scale, (Xv - mean) / scale

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    best = None
    for C in (0.001, 0.01, 0.1):
        m = LogisticRegression(C=C, max_iter=2000, class_weight="balanced").fit(Zt, yt)
        auc = roc_auc_score(yv, m.decision_function(Zv))
        log.info("C=%g val AUROC %.4f", C, auc)
        if best is None or auc > best[0]:
            best = (auc, C, m)
    auc, C, m = best
    mlp = None
    if args.mlp:
        from sklearn.neural_network import MLPClassifier

        bm = None
        for alpha in (1e-4, 1e-3, 1e-2):
            net = MLPClassifier(hidden_layer_sizes=(args.mlp,), alpha=alpha, early_stopping=True,
                                random_state=args.seed, max_iter=200).fit(Zt, yt)
            p_ = np.clip(net.predict_proba(Zv)[:, 1], 1e-7, 1 - 1e-7)
            a_ = roc_auc_score(yv, np.log(p_ / (1 - p_)))
            log.info("MLP alpha=%g val AUROC %.4f", alpha, a_)
            if bm is None or a_ > bm[0]:
                bm = (a_, alpha, net)
        auc, C, net = bm
        mlp = {"W1": net.coefs_[0].astype(np.float32), "b1": net.intercepts_[0].astype(np.float32),
               "W2": net.coefs_[1][:, 0].astype(np.float32), "b2": np.float32(net.intercepts_[1][0])}
        h = np.maximum(Zv @ mlp["W1"] + mlp["b1"], 0.0)
        zv = h @ mlp["W2"] + mlp["b2"]
    else:
        zv = m.decision_function(Zv)
    pa, pb = fit_platt(zv, yv)
    pv = sigmoid(pa * zv + pb)

    def quantile_thr(pos: np.ndarray, target: float | None = None) -> float:
        target = args.target_recall if target is None else target
        pos = np.sort(pos)
        t = float(pos[int(np.floor((1 - target) * len(pos)))]) if len(pos) else 0.5
        return max(t, 0.05)

    thr = quantile_thr(pv[yv == 1])
    doc_fpr_info = None
    if args.target_doc_fpr is not None:
        neg_val = [i for i, ex in enumerate(exs) if val_docs[i] and not ex.spans]
        doc_max = dict.fromkeys(neg_val, 0.0)
        for d, pr_ in zip(vdoc, pv):
            if d in doc_max:
                doc_max[d] = max(doc_max[d], float(pr_))
        dm = np.sort(np.array(list(doc_max.values())))[::-1]
        k = int(np.floor(args.target_doc_fpr * len(dm)))  # docs allowed to flag
        thr = max(float(np.nextafter(dm[k], 1.0)) if k < len(dm) else 0.05, 0.05)
        doc_fpr_info = {"n_neg_val_docs": len(dm), "neg_val_doc_flag_rate": float((dm >= thr).mean()),
                        "val_candidate_recall": float((pv[yv == 1] >= thr).mean())}
        log.info("doc-FPR threshold %.4f: %s", thr, doc_fpr_info)
    cv = np.array(cls["val"])
    class_thr = None
    if args.per_class_thr:
        class_thr = []
        for c in range(len(labels)):
            pos_c = pv[(yv == 1) & (cv == c)]
            class_thr.append(quantile_thr(pos_c, args.class_target_recall) if len(pos_c) >= args.min_class_pos else thr)
        log.info("per-class thresholds: %s", {lab: round(t, 3) for lab, t in zip(labels, class_thr)})
    keep = pv >= (np.array(class_thr)[cv] if class_thr else thr)
    val_prec = float((yv[keep] == 1).mean()) if keep.any() else None
    st2 = Stage2(mean=mean.astype(np.float32), scale=scale.astype(np.float32), w=m.coef_[0].astype(np.float32),
                 b=float(m.intercept_[0]), platt_a=pa, platt_b=pb, threshold=thr, prop_hi=prop_hi, ctx=args.ctx,
                 onehot_labels=list(labels) if args.onehot else None, prop_lo_floor=args.prop_lo_floor,
                 class_thresholds=class_thr, layer2=args.layer2, mlp=mlp,
                 meta={"C": C, "mlp_hidden": args.mlp, "val_auroc": auc, "val_precision_at_thr": val_prec, "n_train": int(len(yt)),
                       "n_train_pos": int(yt.sum()), "n_val": int(len(yv)), "n_val_pos": int(yv.sum()),
                       "prop_hi": args.prop_hi, "onehot": args.onehot, "prop_lo_floor": args.prop_lo_floor, "target_recall": args.target_recall, "class_target_recall": args.class_target_recall, "target_doc_fpr": args.target_doc_fpr,
                       "doc_fpr_info": doc_fpr_info, "data_hash": dh,
                       "seed": args.seed, "seconds": time.time() - t0})
    st2.save(Path(args.bundle) / "stage2")
    print(json.dumps(st2.meta, indent=1, default=float))


if __name__ == "__main__":
    main()
