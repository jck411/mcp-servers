"""Knowledge server package.

Re-exports core classes and utilities so existing imports like
`from servers.knowledge import KnowledgeSettings` continue to work.
"""

from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.settings import (
    DEFAULT_HTTP_PORT,
    FACT_COLUMNS,
    FACT_TYPES,
    PROJECT_ROOT,
    KnowledgeSettings,
)
from servers.knowledge.temporal import (
    add_fact_temporal_status,
    fact_temporal_counts,
    fact_temporal_status,
)
from servers.knowledge.db import KnowledgeDB, search_fact_keywords
from servers.knowledge.vectors import KnowledgeVectorStore
from servers.knowledge.search import (
    classify_search_temporal_intent,
    resolve_search_domains,
    search_knowledge,
)
from servers.knowledge.wiki import preview_wiki_rebuild, rebuild_wiki, wiki_lint_pass
from servers.knowledge.curation import (
    apply_curation_item,
    apply_curation_pack_resolution,
    build_curation_question_packs,
    create_curation_queue_item,
    curation_item_has_destructive_actions,
)

__all__ = [
    "BM25SparseEncoder",
    "DEFAULT_HTTP_PORT",
    "EmbeddingClient",
    "FACT_COLUMNS",
    "FACT_TYPES",
    "KnowledgeDB",
    "KnowledgeSettings",
    "KnowledgeVectorStore",
    "PROJECT_ROOT",
    "add_fact_temporal_status",
    "apply_curation_item",
    "apply_curation_pack_resolution",
    "build_curation_question_packs",
    "classify_search_temporal_intent",
    "create_curation_queue_item",
    "curation_item_has_destructive_actions",
    "fact_temporal_counts",
    "fact_temporal_status",
    "preview_wiki_rebuild",
    "rebuild_wiki",
    "resolve_search_domains",
    "search_knowledge",
    "search_fact_keywords",
    "wiki_lint_pass",
]
