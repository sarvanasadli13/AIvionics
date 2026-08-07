"""Embedding providers (PLAN 2.3).

Two implementations behind one protocol:

  FastEmbedder  real vectors from fastembed / ONNX, model from config.EMBED_MODEL.
  FakeEmbedder  deterministic hashed bag-of-words, no network, no model file.
                Used by the unit tests so pytest never downloads anything.

Every vector carries the embedder's ``index_version`` when it is stored, so a
model change invalidates the stored vectors instead of silently mixing spaces
(standing rule 9).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from .. import config

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def model_cache_dir() -> Path:
    """Where downloaded model files live. Outside the repo, never committed."""
    env = os.environ.get("AIVIONICS_MODEL_CACHE")
    return Path(env) if env else config.DATA_DIR / "models"


def l2_normalise(mat: np.ndarray) -> np.ndarray:
    """Row-wise unit length, so a dot product is a cosine similarity."""
    mat = np.asarray(mat, dtype=np.float32)
    if mat.size == 0:
        return mat
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (mat / norms).astype(np.float32)


def vec_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


class Embedder(Protocol):
    """What the indexer and the searcher are allowed to assume."""

    dim: int
    model_name: str
    index_version: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """(n, dim) float32, L2-normalised, one row per input in order."""
        ...


class FakeEmbedder:
    """Deterministic hashed bag-of-words. Offline, dependency-free, and — unlike
    a hash of the whole string — it preserves lexical similarity, so ranking
    tests measure ranking rather than luck."""

    def __init__(
        self,
        dim: int = config.EMBED_DIM,
        index_version: str = "v0-fake",
        model_name: str = "fake-hash",
    ) -> None:
        self.dim = dim
        self.index_version = index_version
        self.model_name = model_name

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for tok in _TOKEN_RE.findall((text or "").lower()):
            digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=16).digest()
            idx = int.from_bytes(digest[:8], "big") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[idx] += sign
        return vec

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return l2_normalise(np.stack([self._one(t) for t in texts]))


class FastEmbedder:
    """fastembed TextEmbedding. The model is loaded lazily, so constructing one
    in a test collection phase costs nothing."""

    def __init__(
        self,
        model_name: str = config.EMBED_MODEL,
        dim: int = config.EMBED_DIM,
        index_version: str = config.INDEX_VERSION,
        cache_dir: Path | str | None = None,
        batch_size: int = 64,
        threads: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.dim = dim
        self.index_version = index_version
        self.cache_dir = Path(cache_dir) if cache_dir else model_cache_dir()
        self.batch_size = batch_size
        self.threads = threads
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self.cache_dir),
                threads=self.threads,
            )
        return self._model

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        texts = [t if t else " " for t in texts]
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        raw = list(self.model.embed(texts, batch_size=self.batch_size))
        mat = np.asarray(raw, dtype=np.float32)
        if mat.shape[1] != self.dim:
            raise ValueError(
                f"{self.model_name} returned dim {mat.shape[1]}, "
                f"config.EMBED_DIM is {self.dim} — bump INDEX_VERSION and fix config"
            )
        return l2_normalise(mat)
