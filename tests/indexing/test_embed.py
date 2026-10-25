import math
import os
import subprocess
import sys
import types

import pytest

from cda.indexing.context_header import context_header
from cda.indexing.embed import BgeEmbedder, HashEmbedder


def test_context_header():
    assert context_header("a.pdf", "Climate > Metrics") == "a.pdf > Climate > Metrics"
    assert context_header("a.pdf", "") == "a.pdf"


def test_hash_vectors():
    embedder = HashEmbedder()
    a, b, c = embedder.embed_passages(
        ["scope 1 emissions", "scope 1 and 2 emissions", "board remuneration"]
    )
    assert len(a) == 384
    assert math.isclose(sum(x * x for x in a), 1)
    assert a == embedder.embed_query("scope 1 emissions")
    assert sum(x * y for x, y in zip(a, b)) > sum(x * y for x, y in zip(a, c))


def test_lazy_import():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import cda.indexing.embed; import sys; assert 'torch' not in sys.modules",
        ],
        check=True,
    )


def test_bge_prefix(monkeypatch):
    calls = []

    class Stub:
        def __init__(self, name, *, device):
            calls.append((name, device))

        def encode(self, texts, **kwargs):
            calls.append((texts, kwargs))
            return types.SimpleNamespace(tolist=lambda: [[1.0] * 384 for _ in texts])

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Stub)
    )
    embedder = BgeEmbedder()
    assert not calls
    embedder.embed_query("climate")
    embedder.embed_passages(["climate"])
    assert calls[0] == ("BAAI/bge-small-en-v1.5", "cpu")
    assert calls[1][0] == ["Represent this sentence for searching relevant passages: climate"]
    assert calls[2][0] == ["climate"]
    assert all(call[1]["normalize_embeddings"] for call in calls[1:])


@pytest.mark.model
@pytest.mark.skipif(os.environ.get("CDA_RUN_MODEL") != "1", reason="model opt-in required")
def test_real_model():
    embedder = BgeEmbedder()
    q = embedder.embed_query("What are our greenhouse gas emissions?")
    relevant, irrelevant = embedder.embed_passages(
        ["Our scope 1 greenhouse gas emissions fell this year.", "The board approved remuneration."]
    )
    for vector in [q, relevant, irrelevant]:
        assert len(vector) == 384
        assert math.isclose(sum(x * x for x in vector), 1, abs_tol=1e-5)
    assert sum(x * y for x, y in zip(q, relevant)) > sum(x * y for x, y in zip(q, irrelevant))
