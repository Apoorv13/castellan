"""Command line entry point.

    castellan synth  --n 20000 --seed 0 --out data_out/train.jsonl  [--lookalikes 5000]
    castellan train  --data data_out/train.jsonl --model Qwen/Qwen3-1.7B --revision <sha> --out bundles/q17
    castellan eval   --bundle bundles/q17 --data eval_data/dev.jsonl --baselines gitleaks,presidio --out runs/dev.json
    castellan scan   --bundle bundles/q17 file.txt        (or - for stdin; --json for tool-call guard mode)

Output never includes matched values unless ``--show-values`` is passed to scan.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path


def git_state() -> dict:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip())
        return {"git_sha": sha, "git_dirty": dirty}
    except Exception:
        return {"git_sha": None, "git_dirty": None}


def _read_examples(paths):
    from castellan.data.schema import read_jsonl

    out = []
    for p in paths:
        out.extend(read_jsonl(p))
    return out


def cmd_synth(a) -> int:
    from castellan.data.schema import write_jsonl
    from castellan.data.synth import generate

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    exs = generate(a.n, seed=a.seed)
    if a.lookalikes:
        from castellan.data.lookalikes import generate_lookalikes

        exs = exs + generate_lookalikes(a.lookalikes, seed=a.seed)
    n = write_jsonl(a.out, exs)
    print(json.dumps({"wrote": n, "out": a.out, "seed": a.seed, "lookalikes": a.lookalikes}))
    return 0


def cmd_train(a) -> int:
    from castellan.train import TrainConfig, train

    exs = _read_examples(a.data)
    if a.max_examples:
        exs = exs[: a.max_examples]
    cfg = TrainConfig(
        model_id=a.model, revision=a.revision, device=a.device, dtype=a.dtype, cache_dir=a.cache_dir,
        seed=a.seed, layers=[int(x) for x in a.layers.split(",")] if a.layers else None,
        epochs=a.epochs, window=a.window, stride=a.stride, batch_size=a.batch_size, target_recall=a.target_recall,
    )
    res = train(exs, cfg)
    res.bundle.meta.update(git_state())
    res.bundle.save(a.out)
    print(json.dumps({"bundle": a.out, "layer": res.layer, "sweep": res.sweep, "val_auroc": res.val_auroc,
                      "data_hash": res.data_hash, "seconds": round(res.seconds, 1)}, indent=1))
    return 0


def cmd_eval(a) -> int:
    from castellan.cache import dataset_hash
    from castellan.evaluate import compare_operating_points, evaluate, run_info, timed_predict

    exs = _read_examples(a.data)
    detectors = []
    count_tokens = None
    if a.bundle:
        from castellan.bundle import ProbeDetector, load_bundle

        b = load_bundle(a.bundle, device=a.device)
        detectors.append(ProbeDetector(b))
        count_tokens = lambda t: len(b.extractor.tokenize(t))  # noqa: E731
    for name in filter(None, (a.baselines or "").split(",")):
        if name == "gitleaks":
            from castellan.baselines.gitleaks import GitleaksDetector

            detectors.append(GitleaksDetector())
        elif name == "presidio":
            from castellan.baselines.presidio import PresidioDetector

            detectors.append(PresidioDetector())
        else:
            raise SystemExit(f"unknown baseline {name!r}")
    report = {"run": {**run_info(), **git_state(), "data": a.data, "data_hash": dataset_hash(exs)}, "detectors": {}}
    all_preds = {}
    for det in detectors:
        t = timed_predict(det.predict, exs, count_tokens)
        all_preds[det.name] = t.preds
        info = {"ms_per_1k_tokens": t.ms_per_1k_tokens, "seconds": t.seconds, "n_tokens": t.n_tokens,
                "peak_rss_bytes": t.peak_rss_bytes, "mps_driver_bytes": t.mps_driver_bytes}
        if det.name == "probe":
            bb = det.bundle
            info.update({"model_id": bb.model_id, "revision": bb.revision, "dtype": bb.dtype, "layer": bb.layer,
                         "train_data_hash": bb.meta.get("data_hash"), "seed": bb.meta.get("seed")})
        else:
            info["version"] = getattr(det, "version", None)
            info["display_name"] = getattr(det, "display_name", det.name)
            if hasattr(det, "recognizer_config"):
                info["recognizers"] = det.recognizer_config()
                info["spacy_model"] = det.spacy_model
        report["detectors"][det.name] = {"info": info, "metrics": evaluate(exs, t.preds)}
    if "probe" in all_preds and len(all_preds) > 1:
        report["comparisons"] = {
            name: compare_operating_points(exs, all_preds["probe"], preds)
            for name, preds in all_preds.items()
            if name != "probe"
        }
    text = json.dumps(report, indent=1, default=float)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
    summary = {name: {cat: {k: d["metrics"]["categories"][cat][k] for k in ("span_precision", "span_recall", "char_precision", "doc_auroc")}
                      for cat in ("secret", "pattern_pii")} for name, d in report["detectors"].items()}
    print(json.dumps(summary, indent=1))
    return 0


def cmd_scan(a) -> int:
    from castellan.bundle import load_bundle

    b = load_bundle(a.bundle, device=a.device)
    raw = sys.stdin.read() if a.input == "-" else Path(a.input).read_text(encoding="utf-8")
    if a.json:
        from castellan.guard import guard

        obj = json.loads(raw)
        d = guard(obj, b.scan)
        out = d.to_dict()
        if a.show_values:
            from castellan.guard import serialize

            leaves = {l.path: l.raw for l in serialize(obj)[1]}
            for f, fd in zip(d.findings, out["findings"]):
                fd["value"] = leaves[f.path][f.start : f.end]
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    findings = b.scan(raw)
    out = []
    for f in findings:
        fd = f.to_dict()
        if a.show_values:
            fd["value"] = raw[f.start : f.end]
        out.append(fd)
    print(json.dumps({"findings": out}, indent=1, ensure_ascii=False))
    return 0


def cmd_readability(a) -> int:
    from castellan.data.schema import read_jsonl
    from castellan.readability import run

    res = run(list(read_jsonl(a.data)), a.model, a.revision, a.out, device=a.device, dtype=a.dtype,
              batch_size=a.batch_size, max_new_tokens=a.max_new_tokens)
    print(json.dumps(res))  # rates and counts only, never model output
    return 0


def cmd_later(a) -> int:
    print(f"'{a.cmd}' lands in a later milestone (ladder: M2, readability: M3).", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="castellan")
    p.add_argument("--version", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("synth", help="generate synthetic training data")
    s.add_argument("--n", type=int, default=20000)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--lookalikes", type=int, default=0,
                   help="also append N benign lookalike documents (data/lookalikes.py), own RNG stream")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_synth)

    t = sub.add_parser("train", help="train a probe bundle")
    t.add_argument("--data", nargs="+", required=True)
    t.add_argument("--model", required=True)
    t.add_argument("--revision", required=True, help="pinned commit sha, or 'local' for a local path")
    t.add_argument("--device", default="mps")
    t.add_argument("--dtype", default="bf16", choices=["bf16", "fp32", "fp16"])
    t.add_argument("--cache-dir", default="cache")
    t.add_argument("--out", required=True)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--layers", default=None, help="comma list; default is an 8-layer sweep")
    t.add_argument("--epochs", type=int, default=4)
    t.add_argument("--window", type=int, default=512)
    t.add_argument("--stride", type=int, default=384)
    t.add_argument("--batch-size", type=int, default=8)
    t.add_argument("--target-recall", type=float, default=0.95)
    t.add_argument("--max-examples", type=int, default=None)
    t.set_defaults(fn=cmd_train)

    e = sub.add_parser("eval", help="evaluate a bundle and baselines")
    e.add_argument("--bundle", default=None)
    e.add_argument("--data", nargs="+", required=True)
    e.add_argument("--baselines", default="")
    e.add_argument("--device", default="mps")
    e.add_argument("--out", default=None)
    e.set_defaults(fn=cmd_eval)

    c = sub.add_parser("scan", help="scan text (or a JSON tool call with --json)")
    c.add_argument("input", help="file path, or - for stdin")
    c.add_argument("--bundle", required=True)
    c.add_argument("--device", default="mps")
    c.add_argument("--json", action="store_true", help="treat input as a tool call and apply the guard")
    c.add_argument("--show-values", action="store_true", help="include matched values in output")
    c.set_defaults(fn=cmd_scan)

    r = sub.add_parser("readability", help="model_can_read per ladder secret item (outputs never logged)")
    r.add_argument("--data", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--revision", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--device", default="mps")
    r.add_argument("--dtype", default="bf16")
    r.add_argument("--batch-size", type=int, default=8)
    r.add_argument("--max-new-tokens", type=int, default=1024)
    r.set_defaults(fn=cmd_readability)
    sub.add_parser("ladder", help="later milestone").set_defaults(fn=cmd_later)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if a.version:
        from castellan import __version__

        print(__version__)
        return 0
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
