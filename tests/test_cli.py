"""CLI end to end on the tiny model: synth -> train -> eval -> scan."""

from __future__ import annotations

import json

import pytest

from _tiny import make_examples
from castellan.cli import main
from castellan.data.schema import Example, Span, write_jsonl

pytest.importorskip("torch")


def test_synth(tmp_path, capsys):
    out = tmp_path / "s.jsonl"
    assert main(["synth", "--n", "20", "--out", str(out)]) == 0
    assert len(out.read_text().splitlines()) == 20


def test_train_eval_scan(tiny_model, tmp_path, capsys):
    train = tmp_path / "train.jsonl"
    test = tmp_path / "test.jsonl"
    write_jsonl(train, make_examples(300, seed=20))
    write_jsonl(test, make_examples(40, seed=21))
    bundle = tmp_path / "bundle"
    rc = main(["train", "--data", str(train), "--model", str(tiny_model.path), "--revision", "local",
               "--device", "cpu", "--dtype", "fp32", "--cache-dir", str(tmp_path / "c"), "--out", str(bundle)])
    assert rc == 0
    capsys.readouterr()

    report = tmp_path / "r.json"
    assert main(["eval", "--bundle", str(bundle), "--data", str(test), "--device", "cpu", "--out", str(report)]) == 0
    r = json.loads(report.read_text())
    m = r["detectors"]["probe"]["metrics"]["categories"]["secret"]
    assert m["span_recall"] > 0.9 and m["span_precision"] > 0.9
    assert r["detectors"]["probe"]["info"]["layer"] in range(4)
    assert "comparisons" not in r  # no baseline requested
    capsys.readouterr()

    import shutil

    if shutil.which("gitleaks"):
        assert main(["eval", "--bundle", str(bundle), "--data", str(test), "--device", "cpu",
                     "--baselines", "gitleaks", "--out", str(report)]) == 0
        r = json.loads(report.read_text())
        c = r["comparisons"]["gitleaks"]["categories"]["secret"]
        assert {"baseline_recall", "baseline_fpr", "probe_recall_at_baseline_fpr", "probe_fpr_at_baseline_recall"} <= set(c)
        capsys.readouterr()

    ex = make_examples(30, seed=22)
    ex = next(e for e in ex if e.spans)
    secret = ex.text[ex.spans[0].start : ex.spans[0].end]
    f = tmp_path / "in.txt"
    f.write_text(ex.text)
    assert main(["scan", str(f), "--bundle", str(bundle), "--device", "cpu"]) == 0
    out = capsys.readouterr().out
    assert secret not in out and json.loads(out)["findings"]
    assert main(["scan", str(f), "--bundle", str(bundle), "--device", "cpu", "--show-values"]) == 0
    assert secret in capsys.readouterr().out

    call = tmp_path / "call.json"
    call.write_text(json.dumps({"name": "run", "input": {"key": secret, "note": "the agent called tool"}}))
    assert main(["scan", str(call), "--bundle", str(bundle), "--device", "cpu", "--json"]) == 0
    out = capsys.readouterr().out
    d = json.loads(out)
    assert d["action"] == "block" and d["findings"][0]["path"] == "$.input.key"
    assert secret not in out
