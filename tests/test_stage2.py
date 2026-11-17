"""Stage-2 span classifier: feature pooling, proposals, save/load, decoding."""

import numpy as np

from castellan.probe import LinearProbe
from castellan.stage2 import Stage2, decode_two_stage, pool_features, propose


def test_pool_features_shape_and_edges():
    acts = np.arange(10 * 4, dtype=np.float32).reshape(10, 4)
    f = pool_features(acts, 0, 2, ctx=3, peak=0.9)
    assert f.shape == (4 * 4 + 2,)
    assert np.allclose(f[8:12], 0)  # no left context at the start
    assert np.allclose(f[12:16], acts[3:6].mean(0))
    f = pool_features(acts, 8, 9, ctx=3, peak=0.5)
    assert np.allclose(f[12:16], 0)  # no right context at the end


def _probe(H=4):
    W = np.zeros((H, 1), np.float32)
    W[0, 0] = 1.0
    return LinearProbe(labels=["SECRET_GENERIC"], mean=np.zeros(H, np.float32), scale=np.ones(H, np.float32),
                       W=W, b=np.zeros(1, np.float32), lo=np.array([0.05]))


def test_propose_and_two_stage_decode(tmp_path):
    text = "aa bb cc dd"
    offsets = [(0, 2), (3, 5), (6, 8), (9, 11)]
    acts = np.zeros((4, 4), np.float32)
    acts[1:3, 0] = 5.0  # tokens 1..2 score high
    acts[:, 0] -= 0.0
    acts[[0, 3], 0] = -5.0
    probe = _probe()
    cands = propose(text, offsets, probe.proba(acts), probe.labels, np.array([0.2]), np.array([0.05]))
    assert [(c.tok_start, c.tok_end) for c in cands] == [(1, 2)]
    D = 4 * 4 + 2
    st = Stage2(mean=np.zeros(D, np.float32), scale=np.ones(D, np.float32), w=np.zeros(D, np.float32), b=3.0,
                platt_a=1.0, platt_b=0.0, threshold=0.5, prop_hi=np.array([0.2]))
    st.save(tmp_path / "s2")
    st = Stage2.load(tmp_path / "s2")
    found, doc = decode_two_stage(text, offsets, acts, probe, st)
    assert [(f.start, f.end) for f in found] == [(3, 8)]
    assert doc[0] > 0.9
    st.b = -3.0  # stage 2 rejects the candidate
    found, doc = decode_two_stage(text, offsets, acts, probe, st)
    assert found == [] and doc[0] < 0.1


def test_extend_to_value_and_merge():
    from castellan.spans import Finding
    from castellan.stage2 import extend_to_value, merge_same_class

    text = 'cfg = {"api_key": "ab12cd34ef56gh78"}\n'
    s = text.index("cd34")
    a, b = extend_to_value(text, s, s + 4)
    assert text[a:b] == "ab12cd34ef56gh78"
    # never crosses a line end or more than max_ext chars
    t2 = "x" * 50 + "\nAAAA" + "B" * 300
    a, b = extend_to_value(t2, 52, 54, max_ext=10)
    assert a == 51 and b == 64
    fs = merge_same_class([Finding(0, 5, "SECRET_GENERIC", 0.6), Finding(5, 9, "SECRET_GENERIC", 0.9),
                           Finding(3, 7, "SECRET_JWT", 0.7)])
    assert [(f.start, f.end, f.label) for f in fs] == [(0, 9, "SECRET_GENERIC"), (3, 7, "SECRET_JWT")]
    assert fs[0].score == 0.9


def test_stage2_mlp_roundtrip(tmp_path):
    import numpy as np

    from castellan.stage2 import Stage2

    r = np.random.default_rng(0)
    d, h = 6, 4
    mlp = {"W1": r.normal(size=(d, h)).astype(np.float32), "b1": r.normal(size=h).astype(np.float32),
           "W2": r.normal(size=h).astype(np.float32), "b2": np.float32(0.3)}
    st = Stage2(mean=np.zeros(d, np.float32), scale=np.ones(d, np.float32), w=np.zeros(d, np.float32), b=0.0,
                platt_a=1.0, platt_b=0.0, threshold=0.5, prop_hi=np.full(2, 0.2), mlp=mlp)
    F = r.normal(size=(5, d)).astype(np.float32)
    want = np.maximum(F @ mlp["W1"] + mlp["b1"], 0) @ mlp["W2"] + mlp["b2"]
    assert np.allclose(st.logit(F), want, atol=1e-5)
    st.save(tmp_path / "s2")
    st2 = Stage2.load(tmp_path / "s2")
    assert np.allclose(st2.proba(F), st.proba(F), atol=1e-6)
