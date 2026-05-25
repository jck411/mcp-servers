"""Knowledge server configuration from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

FACT_COLUMNS = (
    "domain, key, value, source, confidence, valid_from, valid_until, as_of, "
    "review_after, origin_type, origin_ref, last_confirmed_at, updated_at"
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9017


class KnowledgeSettings(BaseSettings):
    """Knowledge server configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Knowledge storage
    knowledge_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "knowledge",
        validation_alias="KNOWLEDGE_PATH",
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

    # Chunking
    chunk_max_chars: int = Field(default=1000, validation_alias="KNOWLEDGE_CHUNK_MAX_CHARS")
    chunk_overlap: int = Field(default=200, validation_alias="KNOWLEDGE_CHUNK_OVERLAP")

    # OCR for images and scanned PDFs
    ocr_enabled: bool = Field(default=True, validation_alias="KNOWLEDGE_OCR_ENABLED")
    ocr_language: str = Field(default="eng", validation_alias="KNOWLEDGE_OCR_LANGUAGE")

    # Vision LLM used for high-accuracy OCR (set to empty to disable and use tesseract).
    # Any OpenRouter vision-capable model id works, e.g.:
    #   google/gemini-2.0-flash-001  (cheap, fast, very good)
    #   anthropic/claude-3.5-sonnet   (best on dense docs/handwriting)
    #   openai/gpt-4o-mini            (cheap)
    vision_model: str = Field(
        default="google/gemini-2.0-flash-001",
        validation_alias="KNOWLEDGE_VISION_MODEL",
    )
    vision_max_pages: int = Field(default=20, validation_alias="KNOWLEDGE_VISION_MAX_PAGES")
    vision_dpi: int = Field(default=200, validation_alias="KNOWLEDGE_VISION_DPI")

    # Model for single-shot fact extraction via POST /api/sources/{id}/extract.
    # Must be a vision-capable model; Sonnet gives best accuracy on documents.
    extraction_model: str = Field(
        default="anthropic/claude-sonnet-4-6",
        validation_alias="KNOWLEDGE_EXTRACTION_MODEL",
    )

    # Public REST API base used when MCP tools generate clickable download URLs
    api_base: str = Field(
        default="https://api-knowledge.jackshome.com",
        validation_alias="API_BASE",
    )
