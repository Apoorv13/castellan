"""Tiny random Qwen3 model and tokenizer with a planted signal, built offline.

How the planted signal works:
- Benign text uses only lowercase letters, spaces and a little punctuation.
- Secret values use only SECRET_CHARS (uppercase consonants and digits),
  in chunks joined by separators such as "-", "/" and ",".
- After training a byte-level BPE tokenizer on a mixed corpus, every token
  whose decoded string contains a SECRET_CHARS character is a "secret token".
  By construction these only occur inside labeled secret spans.
- We add ALPHA * v to the embedding row of every secret token. The direction
  then flows through the real residual stream and the real hooks, so the
  pipeline must recover it from activations, with no label leakage.
- Separator tokens inside a span ("-", "/", ",") do NOT get the direction.
  A span decoder that does not bridge low-scoring punctuation and whitespace
  will fragment these spans. That is the regression the pipeline test guards.

Nothing here resembles a real credential format.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from castellan.data.schema import Example, Span

SECRET_CHARS = "BCDFGHJKLMNPQRSTVWXZ0123456789"
SEPARATORS = ("-", "/", ",", ".")
WORDS = (
    "the agent called tool with args and got result from server config file "
    "value user request response log entry error stack trace line header path "
    "service deploy build test run job step output input token count model "
    "please check this before sending next turn ok done failed retry later "
    "database connection cache layer window score probe span label class"
).split()
CHAT_TEMPLATE = (
    "{% for m in messages %}<|im_start|>{{ m['role'] }}\n{{ m['content'] }}<|im_end|>\n{% endfor %}"
    "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
)
ALPHA = 1.0
NUM_LAYERS = 4
HIDDEN = 64


@dataclass
class TinyModel:
    path: Path
    secret_token_ids: list[int]
    direction: np.ndarray  # unit vector, shape (HIDDEN,)


def make_secret_value(rng: random.Random) -> str:
    n_chunks = rng.randint(2, 4)
    chunks = ["".join(rng.choice(SECRET_CHARS) for _ in range(rng.randint(3, 6))) for _ in range(n_chunks)]
    out = chunks[0]
    for c in chunks[1:]:
        out += rng.choice(SEPARATORS) + c
    return out


def make_benign_text(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n_words))


def make_example(rng: random.Random, idx: int, p_secret: float = 0.6) -> Example:
    pre = make_benign_text(rng, rng.randint(3, 12))
    post = make_benign_text(rng, rng.randint(3, 12))
    if rng.random() < p_secret:
        val = make_secret_value(rng)
        text = f"{pre} key {val} {post}"
        start = len(pre) + len(" key ")
        spans = [Span(start, start + len(val), "SECRET_GENERIC")]
    else:
        text = f"{pre} key none {post}"
        spans = []
    return Example(id=f"tiny-{idx}", text=text, spans=spans, carrier="prose", source="synth")


def make_examples(n: int, seed: int) -> list[Example]:
    rng = random.Random(seed)
    return [make_example(rng, i) for i in range(n)]


def build_tiny_model(out_dir: Path, seed: int = 0) -> TinyModel:
    """Train a tokenizer, build a random Qwen3, plant the direction, save both."""
    import torch
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, processors, trainers
    from transformers import PreTrainedTokenizerFast, Qwen3Config, Qwen3ForCausalLM

    rng = random.Random(seed)
    corpus = [make_example(rng, i).text for i in range(3000)]

    specials = ["<|endoftext|>", "<|im_start|>", "<|im_end|>"]
    tok = Tokenizer(models.BPE())
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    # Match Qwen3's tokenizer.json: offsets keep the leading space of a token.
    tok.post_processor = processors.ByteLevel(trim_offsets=False)
    trainer = trainers.BpeTrainer(
        vocab_size=600,
        special_tokens=specials,
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=False,
    )
    tok.train_from_iterator(corpus, trainer=trainer)

    hf_tok = PreTrainedTokenizerFast(
        tokenizer_object=tok,
        eos_token="<|im_end|>",
        pad_token="<|endoftext|>",
        additional_special_tokens=["<|im_start|>"],
    )
    hf_tok.chat_template = CHAT_TEMPLATE

    torch.manual_seed(seed)
    cfg = Qwen3Config(
        vocab_size=len(hf_tok),
        hidden_size=HIDDEN,
        intermediate_size=128,
        num_hidden_layers=NUM_LAYERS,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=16,
        max_position_embeddings=1024,
        tie_word_embeddings=False,
        eos_token_id=hf_tok.eos_token_id,
        pad_token_id=hf_tok.pad_token_id,
    )
    model = Qwen3ForCausalLM(cfg).eval()

    secret_set = set(SECRET_CHARS)
    secret_ids = sorted(
        i
        for i in range(len(hf_tok))
        if secret_set & set(hf_tok.decode([i]))
    )
    g = np.random.default_rng(seed)
    v = g.standard_normal(HIDDEN)
    v /= np.linalg.norm(v)
    with torch.no_grad():
        emb = model.model.embed_tokens.weight
        emb[secret_ids] += ALPHA * torch.tensor(v, dtype=emb.dtype)

    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out_dir)
    hf_tok.save_pretrained(out_dir)
    np.save(out_dir / "planted_direction.npy", v)
    return TinyModel(path=out_dir, secret_token_ids=secret_ids, direction=v)
