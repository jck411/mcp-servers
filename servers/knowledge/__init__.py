"""Knowledge server package.

Re-exports core classes and utilities so existing imports like
`from servers.knowledge import KnowledgeSettings` continue to work
during the migration from the monolithic knowledge.py file.

Phase 1: settings.py, embeddings.py, temporal.py
Phase 2: db.py, vectors.py
Phase 3: extraction.py, ingestion.py, wiki.py, curation.py, search.py, sources.py
"""

# --- Phase 1 ---
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

# --- Phase 2 ---
from servers.knowledge.db import KnowledgeDB, search_fact_keywords
from servers.knowledge.vectors import KnowledgeVectorStore

# --- Phase 3 ---
from servers.knowledge.extraction import (
    _is_likely_binary,
    chunk_text,
    compute_text_hash,
)
from servers.knowledge.ingestion import _ingest_file_at_path, _validate_text_ingest_inputs
from servers.knowledge.sources import (
    delete_source_record,
    delete_sources_for_overwrite,
    rename_source_record,
    source_download_bytes,
)
from servers.knowledge.search import (
    classify_search_temporal_intent,
    resolve_search_domains,
    search_knowledge,
)
from servers.knowledge.wiki import preview_wiki_rebuild, rebuild_wiki
from servers.knowledge.curation import (
    apply_curation_item,
    apply_curation_pack_resolution,
    build_curation_question_packs,
    create_curation_queue_item,
    curation_item_has_destructive_actions,
)

__all__ = [
    # Phase 1
    "BM25SparseEncoder",
    "DEFAULT_HTTP_PORT",
    "EmbeddingClient",
    "FACT_COLUMNS",
    "KnowledgeSettings",
    "PROJECT_ROOT",
    "add_fact_temporal_status",
    "fact_temporal_counts",
    "fact_temporal_status",
    # Phase 2
    "KnowledgeDB",
    "KnowledgeVectorStore",
    "search_fact_keywords",
    # Phase 3
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
