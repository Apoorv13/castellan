"""Checks on the tiny model fixture itself, plus the layer indexing convention."""

from __future__ import annotations

import random

import numpy as np
import pytest

from _tiny import NUM_LAYERS, SECRET_CHARS, make_example, make_examples

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")


def _load(tiny):
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(tiny.path)
    model = AutoModelForCausalLM.from_pretrained(tiny.path, dtype=torch.float32).eval()
    return tok, model


def test_tokenizer_offsets_and_chat_template(tiny_model):
    tok, _ = _load(tiny_model)
    ex = make_example(random.Random(1), 0, p_secret=1.0)
    enc = tok(ex.text, return_offsets_mapping=True)
    offs = enc["offset_mapping"]
    assert offs[-1][1] == len(ex.text)
    rendered = tok.apply_chat_template(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}], tokenize=False
    )
    assert "<|im_start|>user" in rendered and "<|im_end|>" in rendered


def test_secret_spans_contain_unplanted_separator_tokens(tiny_model):
    """The fixture must exercise span bridging: some tokens inside spans lack the signal."""
    tok, _ = _load(tiny_model)
    secret_ids = set(tiny_model.secret_token_ids)
    found_gap = False
    for ex in make_examples(50, seed=2):
        if not ex.spans:
            continue
        s = ex.spans[0]
        enc = tok(ex.text, return_offsets_mapping=True)
        inside = [
            tid for tid, (a, b) in zip(enc["input_ids"], enc["offset_mapping"]) if a >= s.start and b <= s.end
        ]
        if any(t not in secret_ids for t in inside):
            found_gap = True
            break
    assert found_gap


def test_secret_tokens_never_appear_outside_spans(tiny_model):
    tok, _ = _load(tiny_model)
    secret_ids = set(tiny_model.secret_token_ids)
    for ex in make_examples(100, seed=3):
        enc = tok(ex.text, return_offsets_mapping=True)
        for tid, (a, b) in zip(enc["input_ids"], enc["offset_mapping"]):
            if tid in secret_ids:
                # Offsets include a token's leading space (Qwen3 behavior), so trim it.
                a += len(ex.text[a:b]) - len(ex.text[a:b].lstrip())
                assert any(a >= s.start and b <= s.end for s in ex.spans)


@pytest.mark.parametrize("k", range(NUM_LAYERS - 1))
def test_layer_k_output_equals_hidden_states_k_plus_1(tiny_model, k):
    """Design decision 1: layer k means model.model.layers[k] output == hidden_states[k+1].

    The last entry of hidden_states has the final norm applied in Qwen3, so the
    identity holds for every layer except the deepest one. Hooks are the source
    of truth for the raw residual stream.
    """
    tok, model = _load(tiny_model)
    ids = tok("the agent called tool with args", return_tensors="pt")["input_ids"]
    captured = {}

    def hook(_mod, _inp, out):
        captured["h"] = (out[0] if isinstance(out, tuple) else out).detach()

    h = model.model.layers[k].register_forward_hook(hook)
    try:
        with torch.no_grad():
            out = model(ids, output_hidden_states=True)
    finally:
        h.remove()
    assert len(out.hidden_states) == NUM_LAYERS + 1
    torch.testing.assert_close(captured["h"], out.hidden_states[k + 1])


def test_deepest_layer_hidden_state_is_normed(tiny_model):
    """The last hidden_states entry is norm(layers[-1] output), not the raw residual."""
    tok, model = _load(tiny_model)
    ids = tok("the agent called tool with args", return_tensors="pt")["input_ids"]
    captured = {}

    def hook(_mod, _inp, out):
        captured["h"] = (out[0] if isinstance(out, tuple) else out).detach()

    h = model.model.layers[NUM_LAYERS - 1].register_forward_hook(hook)
    try:
        with torch.no_grad():
            out = model(ids, output_hidden_states=True)
            normed = model.model.norm(captured["h"])
    finally:
        h.remove()
    torch.testing.assert_close(normed, out.hidden_states[-1])
    assert not torch.allclose(captured["h"], out.hidden_states[-1])


def test_planted_direction_is_linearly_recoverable(tiny_model):
    """Sanity check that the fixture is learnable, using hidden_states directly."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    tok, model = _load(tiny_model)
    secret_set = set(SECRET_CHARS)
    feats, labels = [], []
    for ex in make_examples(80, seed=4):
        enc = tok(ex.text, return_offsets_mapping=True, return_tensors="pt")
        with torch.no_grad():
            hs = model(enc["input_ids"], output_hidden_states=True).hidden_states[2][0].numpy()
        for row, (a, b) in zip(hs, enc["offset_mapping"][0].tolist()):
            feats.append(row)
            labels.append(int(bool(secret_set & set(ex.text[a:b]))))
    X, y = np.array(feats), np.array(labels)
    n = len(y) * 2 // 3
    clf = LogisticRegression(max_iter=2000).fit(X[:n], y[:n])
    assert roc_auc_score(y[n:], clf.predict_proba(X[n:])[:, 1]) > 0.95
