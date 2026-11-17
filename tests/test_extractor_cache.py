from __future__ import annotations

import numpy as np
import pytest

from _tiny import NUM_LAYERS, make_examples
from castellan.cache import (
    ActivationCache,
    bf16_bits_to_f32,
    dataset_hash,
    f32_to_bf16_bits,
    token_labels,
)
from castellan.data.schema import Span
from castellan.extractor import evenly_spaced_layers, trim_offsets, window_bounds

torch = pytest.importorskip("torch")


@pytest.fixture(scope="module")
def extractor(tiny_model):
    from castellan.extractor import Extractor

    return Extractor(str(tiny_model.path), revision="local", device="cpu", dtype="fp32", window=16, stride=12)


def test_window_bounds_cover_everything():
    assert window_bounds(5, 16, 12) == [(0, 5)]
    b = window_bounds(40, 16, 12)
    assert b[0] == (0, 16) and b[-1][1] == 40
    for (a1, b1), (a2, _) in zip(b, b[1:]):
        assert a2 < b1  # overlap


def test_trim_offsets():
    assert trim_offsets("a  bc", [(0, 1), (1, 5)]) == [(0, 1), (3, 5)]


def test_evenly_spaced_layers():
    assert evenly_spaced_layers(4, 8) == [0, 1, 2, 3]
    ls = evenly_spaced_layers(36, 8)
    assert len(ls) == 8 and ls[0] == 0 and ls[-1] == 35


def test_bf16_roundtrip_close():
    x = np.random.default_rng(0).standard_normal((10, 7)).astype(np.float32) * 100
    y = bf16_bits_to_f32(f32_to_bf16_bits(x))
    np.testing.assert_allclose(x, y, rtol=1e-2)
    np.testing.assert_array_equal(y, bf16_bits_to_f32(f32_to_bf16_bits(y)))


def test_token_labels():
    lab = token_labels([(0, 3), (4, 8), (8, 9), (10, 12)], [Span(4, 9, "SECRET_JWT")])
    assert list(lab != 0) == [False, True, True, False]


def test_hooks_match_hidden_states_and_stop_early(extractor):
    ids = extractor.tokenize("the agent called tool with args").input_ids
    (acts,) = extractor.run_batch([ids], [0, 2])
    with torch.no_grad():
        hs = extractor.model(torch.tensor([ids]), output_hidden_states=True).hidden_states
    np.testing.assert_allclose(acts[2], hs[3][0].numpy(), rtol=1e-5, atol=1e-5)

    calls = []
    h = extractor.layers[3].register_forward_hook(lambda *a: calls.append(1))
    try:
        extractor.run_batch([ids], [1])
    finally:
        h.remove()
    assert calls == [], "layers past the deepest requested one must not run"


def test_padding_does_not_change_activations(extractor):
    a = extractor.tokenize("the agent called tool").input_ids
    b = extractor.tokenize("the agent called tool with args and got result from server").input_ids
    alone = extractor.run_batch([a], [2])[0][2]
    batched = extractor.run_batch([a, b], [2])[0][2]
    np.testing.assert_allclose(alone, batched, rtol=1e-4, atol=1e-4)


def test_windowed_extract_matches_full_pass_for_first_window(extractor):
    text = " ".join(["the agent called tool with args"] * 8)
    tok, acts = extractor.extract(text, [1])
    assert len(tok) > extractor.window
    assert acts[1].shape == (len(tok), extractor.hidden_size)
    full = extractor.run_batch([tok.input_ids[: extractor.window]], [1])[0][1]
    np.testing.assert_allclose(acts[1][: extractor.window], full, rtol=1e-5, atol=1e-5)
    assert np.abs(acts[1]).sum(axis=1).min() > 0  # every token filled


def test_cache_build_and_reuse(extractor, tmp_path):
    exs = make_examples(20, seed=5)
    cache = ActivationCache(tmp_path / "c")
    h = dataset_hash(exs)
    out = cache.build(extractor, exs, [1, 3], h)
    lc = out[3]
    total = sum(len(extractor.tokenize(e.text)) for e in exs)
    assert lc.n_tokens == total and lc.labels.shape == (total,)
    assert (lc.labels > 0).any()
    # compare a document against a direct extract
    tok, acts = extractor.extract(exs[0].text, [3])
    np.testing.assert_allclose(lc.read(np.arange(len(tok))), acts[3], rtol=1e-2, atol=1e-2)
    # second build is a cache hit (file not rewritten)
    mtime = lc.path.stat().st_mtime_ns
    cache.build(extractor, exs, [3], h)
    assert lc.path.stat().st_mtime_ns == mtime
    assert cache.get(extractor.model_id, "other-rev", "fp32", 3, h) is None
    # shuffled batches cover the index set exactly once
    idx = np.arange(lc.n_tokens)
    seen = np.concatenate([b for _, b in lc.iter_batches(idx, 17, np.random.default_rng(0))])
    assert sorted(seen.tolist()) == idx.tolist()
    assert NUM_LAYERS == 4
