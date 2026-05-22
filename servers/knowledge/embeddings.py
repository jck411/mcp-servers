"""Embedding clients for Knowledge vector search.

Provides dense embeddings via OpenRouter API and sparse BM25 vectors
for hybrid search in Qdrant.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter

import httpx

from servers.knowledge.settings import KnowledgeSettings


class EmbeddingClient:
    """Generate text embeddings via OpenRouter API."""

    def __init__(self, settings: KnowledgeSettings) -> None:
        self._api_key = settings.openrouter_api_key
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._url = "https://openrouter.ai/api/v1/embeddings"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one API call."""
        if not texts:
            return []
        client = await self._get_client()
        payload: dict = {"model": self._model, "input": texts}
        if "text-embedding-3" in self._model:
            payload["dimensions"] = self._dimensions

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
                data = response.json()
                if "data" not in data:
                    err = data.get("error", data)
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Embedding API error: {msg}")
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in sorted_data]
            except (httpx.HTTPStatusError, httpx.TransportError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError(f"Embedding failed after 3 attempts: {last_error}")

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class BM25SparseEncoder:
    """BM25-based sparse vectors for hybrid search via feature hashing."""

    def __init__(self, vocab_size: int = 30000) -> None:
        self._vocab_size = vocab_size
        self._k1 = 1.5
        self._b = 0.75
        self._doc_count = 0
        self._doc_freqs: Counter[int] = Counter()
        self._avg_doc_len = 0.0
        self._total_doc_len = 0

    def _tokenize(self, text: str) -> list[str]:
        text = text.lower()
        tokens = re.findall(r"\b[a-z0-9]+\b", text)
        return [t for t in tokens if len(t) > 1]

    def _hash_token(self, token: str) -> int:
        h = hashlib.sha256(token.encode()).digest()
        return int.from_bytes(h[:4], "little") % self._vocab_size

    def fit_batch(self, texts: list[str]) -> None:
        for text in texts:
            tokens = self._tokenize(text)
            self._doc_count += 1
            self._total_doc_len += len(tokens)
            unique_indices = set(self._hash_token(t) for t in tokens)
            for idx in unique_indices:
                self._doc_freqs[idx] += 1
        if self._doc_count > 0:
            self._avg_doc_len = self._total_doc_len / self._doc_count

    def encode(self, text: str) -> tuple[list[int], list[float]]:
        tokens = self._tokenize(text)
        if not tokens:
            return [], []
        doc_len = len(tokens)
        term_freqs: Counter[int] = Counter()
        for token in tokens:
            term_freqs[self._hash_token(token)] += 1

        indices = []
        values = []
        for idx, tf in term_freqs.items():
            tf_score = (tf * (self._k1 + 1)) / (
                tf + self._k1 * (1 - self._b + self._b * doc_len / max(self._avg_doc_len, 1))
            )
            df = self._doc_freqs.get(idx, 0)
            idf = max(0.0, (self._doc_count - df + 0.5) / (df + 0.5))
            if idf > 0:
                idf = (idf + 1.0) ** 0.5
            score = tf_score * idf
            if score > 0:
                indices.append(idx)
                values.append(float(score))

        if indices:
            sorted_pairs = sorted(zip(indices, values, strict=True), key=lambda x: x[0])
            indices, values = zip(*sorted_pairs, strict=True)
            return list(indices), list(values)
        return [], []

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        return self.encode(text)
