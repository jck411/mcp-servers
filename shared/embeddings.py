"""OpenRouter-based embedding client for memory server."""

from __future__ import annotations

import asyncio

import httpx

from shared.memory_config import MemorySettings


class EmbeddingClient:
    """Generate text embeddings via OpenRouter API."""

    def __init__(self, settings: MemorySettings) -> None:
        self._api_key = settings.openrouter_api_key.get_secret_value()
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._url = "https://openrouter.ai/api/v1/embeddings"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string. Return a vector."""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one API call (max ~100)."""
        client = await self._get_client()

        payload: dict = {
            "model": self._model,
            "input": texts,
        }
        # Only include dimensions for models that support it
        if "text-embedding-3" in self._model:
            payload["dimensions"] = self._dimensions

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
                data = response.json()
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in sorted_data]
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError(f"Embedding failed after 3 attempts: {last_error}")

    async def close(self) -> None:
        """Shut down the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
