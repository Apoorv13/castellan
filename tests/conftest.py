from __future__ import annotations

import os

import pytest

# Tests are offline and CPU only.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


@pytest.fixture(scope="session")
def tiny_model(tmp_path_factory):
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from _tiny import build_tiny_model

    return build_tiny_model(tmp_path_factory.mktemp("tiny_qwen3"), seed=0)
