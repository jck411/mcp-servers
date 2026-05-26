"""Knowledge server configuration from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

FACT_COLUMNS = (
    "domain, key, value, source, confidence, valid_from, valid_until, as_of, "
    "review_after, origin_type, origin_ref, last_confirmed_at, updated_at, "
    "type, tags"
)

# Valid fact types — controls what the LLM can classify facts as.
FACT_TYPES = frozenset({
    "task",        # Actionable item with status tracking
    "event",       # Time-bound occurrence (has valid_from/valid_until)
    "plan",        # Aspirational / uncommitted intention
    "preference",  # Durable personal preference
    "identity",    # Permanent or semi-permanent personal facts
    "state",       # Observable current condition (uses as_of)
    "reference",   # Lookup data: contacts, account numbers, etc.
    "note",        # Default — anything that doesn't fit above
})

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9017


class KnowledgeSettings(BaseSettings):
    """Knowledge server configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenRouter embedding API
    openrouter_api_key: str = Field(..., validation_alias="OPENROUTER_API_KEY")
    embedding_model: str = Field(
        default="openai/text-embedding-3-small",
        validation_alias="EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(default=1536, validation_alias="EMBEDDING_DIMENSIONS")

    # Qdrant vector store
    qdrant_url: str = Field(default="http://127.0.0.1:6333", validation_alias="QDRANT_URL")
    qdrant_collection: str = Field(
        default="knowledge", validation_alias="KNOWLEDGE_QDRANT_COLLECTION"
    )

    # SQLite database
    db_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "data" / "knowledge.db",
        validation_alias="KNOWLEDGE_DB_PATH",
    )

    # Model used by wiki rebuild prompts. The env var name is kept for
    # compatibility with existing deployments.
    extraction_model: str = Field(
        default="anthropic/claude-sonnet-4-6",
        validation_alias="KNOWLEDGE_EXTRACTION_MODEL",
    )
