"""Configuration for memory subsystem (embeddings, Qdrant, SQLite)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MemorySettings(BaseSettings):
    """Load memory config from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenRouter embedding API
    openrouter_api_key: SecretStr = Field(
        ..., validation_alias="OPENROUTER_API_KEY"
    )
    embedding_model: str = Field(
        default="openai/text-embedding-3-small",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(
        default=1536, validation_alias="EMBEDDING_DIMENSIONS"
    )

    # Qdrant vector store
    qdrant_url: str = Field(
        default="http://127.0.0.1:6333", validation_alias="QDRANT_URL"
    )
    qdrant_collection: str = Field(
        default="memories", validation_alias="QDRANT_COLLECTION"
    )

    # SQLite metadata
    memory_db_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "data" / "memory.db",
        validation_alias="MEMORY_DB_PATH",
    )

    # Maintenance
    cleanup_interval_minutes: int = Field(default=15)
    decay_interval_hours: int = Field(default=24)
    min_importance_threshold: float = Field(default=0.1)

    # Memory profiles — each profile gets its own set of tools with isolated storage
    # Creates remember_jack, recall_jack, remember_family, etc.
    memory_profiles: list[str] = Field(
        default_factory=lambda: ["jack", "family"],
    )
