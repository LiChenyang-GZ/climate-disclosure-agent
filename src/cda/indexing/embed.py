"""Local embedding implementations, with no model imports until first use."""

import hashlib
import math
import re
from importlib import import_module
from typing import Any, Protocol


class Embedder(Protocol):
    dim: int

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class BgeEmbedder:
    dim = 384

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        device: str = "cpu",
        batch_size: int = 32,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model: Any = None

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            self._model = import_module("sentence_transformers").SentenceTransformer(
                self.model_name, device=self.device
            )
        vectors = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True)
        return [[float(value) for value in row] for row in vectors.tolist()]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_passages(
            ["Represent this sentence for searching relevant passages: " + text]
        )[0]


class HashEmbedder:
    def __init__(self, dim: int = 384) -> None:
        if dim < 1:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in re.findall(r"\w+", text.casefold()) or [""]:
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:8]) % self.dim] += 1.0
        length = math.sqrt(sum(value * value for value in vector))
        return [value / length for value in vector]
