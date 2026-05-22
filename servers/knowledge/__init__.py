"""Knowledge server package.

Re-exports core classes and utilities so existing imports like
`from servers.knowledge import KnowledgeSettings` continue to work
during the migration from the monolithic knowledge.py file.

Phase 1 extractions (own modules):
  - settings.py: KnowledgeSettings, constants
  - embeddings.py: EmbeddingClient, BM25SparseEncoder
  - temporal.py: fact_temporal_status, add_fact_temporal_status

Phase 2 extractions (own modules):
  - db.py: KnowledgeDB, search_fact_keywords
  - vectors.py: KnowledgeVectorStore

Everything else is re-exported from knowledge_server.py until
extracted in later phases.
"""

# --- Phase 1: Extracted modules ---
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.settings import (
    DEFAULT_HTTP_PORT,
    FACT_COLUMNS,
    PROJECT_ROOT,
    KnowledgeSettings,
)
from servers.knowledge.temporal import (
    add_fact_temporal_status,
    fact_temporal_counts,
    fact_temporal_status,
)

# --- Phase 2: Extracted modules ---
from servers.knowledge.db import KnowledgeDB, search_fact_keywords
from servers.knowledge.vectors import KnowledgeVectorStore

# --- Not yet extracted: re-export from knowledge_server.py ---
from servers.knowledge_server import (  # noqa: E402
    _ingest_file_at_path,
    _is_likely_binary,
    _validate_text_ingest_inputs,
    apply_curation_item,
    apply_curation_pack_resolution,
    build_curation_question_packs,
    chunk_text,
    classify_search_temporal_intent,
    compute_text_hash,
    create_curation_queue_item,
    curation_item_has_destructive_actions,
    delete_source_record,
    delete_sources_for_overwrite,
    preview_wiki_rebuild,
    rebuild_wiki,
    rename_source_record,
    resolve_search_domains,
    search_knowledge,
    source_download_bytes,
)

__all__ = [
    # Phase 1 extractions
    "BM25SparseEncoder",
    "DEFAULT_HTTP_PORT",
    "EmbeddingClient",
    "FACT_COLUMNS",
    "KnowledgeSettings",
    "PROJECT_ROOT",
    "add_fact_temporal_status",
    "fact_temporal_counts",
    "fact_temporal_status",
    # Phase 2 extractions
    "KnowledgeDB",
    "KnowledgeVectorStore",
    "search_fact_keywords",
    # Re-exports from knowledge_server.py
    "_ingest_file_at_path",
    "_is_likely_binary",
    "_validate_text_ingest_inputs",
    "apply_curation_item",
    "apply_curation_pack_resolution",
    "build_curation_question_packs",
    "chunk_text",
    "classify_search_temporal_intent",
    "compute_text_hash",
    "create_curation_queue_item",
    "curation_item_has_destructive_actions",
    "delete_source_record",
    "delete_sources_for_overwrite",
    "preview_wiki_rebuild",
    "rebuild_wiki",
    "rename_source_record",
    "resolve_search_domains",
    "search_knowledge",
    "source_download_bytes",
]
