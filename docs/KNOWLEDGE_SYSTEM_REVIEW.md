# Knowledge System Review

Last reviewed: 2026-05-04

Snapshot evaluation of `servers/knowledge.py` + `servers/knowledge_api.py` and
related modules. Update this doc whenever a meaningful structural change lands.

## Overall: 8.0 / 10

A non-trivial, production-quality personal RAG stack. Punches above what most
weekend projects ship.

## Aspect Scores

| Aspect | Score | Notes |
|---|---|---|
| Architecture | 9 | Clean split: Qdrant vectors, SQLite structured. Hybrid dense + BM25 with RRF fusion. Domain auto-resolution + `core` always-included is a nice UX touch. |
| Code quality | 8 | Type hints, async throughout, focused modules. `KnowledgeDB` is large (700+ lines) — splitting domains/facts/sources/curation into mixins or separate classes would help. |
| Search quality | 7 | Hybrid is good, BM25 warmup on startup is good. Lacks: query expansion, cross-encoder rerank, MMR diversity, relevance feedback. |
| Ingestion pipeline | 9 | Multi-stage with structured logging, vision OCR with tesseract fallback, image-description path for photos, single-shot fact extraction. Hash-based dedup with stored-path backfill is mature. |
| Robustness | 7 | Good: WAL, FK enforcement, busy_timeout, hash dedup, secure path resolution, token-based downloads. Gaps: no Qdrant↔SQLite reconciliation tool, no transaction-spanning ingest. |
| Observability | 7 | Stdlib `logging` via `shared/logging_config.py`. Every MCP tool wrapped with `@logged_tool` decorator emitting `tool=<name> status=ok|error duration_ms=N` plus result summary. `GET /api/health` reports Qdrant reachability, source/chunk counts, BM25 state. Still missing: counters/metrics, structured (JSON) logs, tracing. |
| Security | 7 | Path traversal blocked, token-gated downloads, auth token on REST. Watch: token cleanup runs on every read (cheap DoS vector), no rate limiting, single-user (intentional). |
| API ergonomics | 8 | MCP tools well-named and documented. Pre-formatted `download_markdown` is great for chat agents. REST and MCP stay in sync via shared functions. |
| Schema design | 8 | Reasonable normalization. Minor `id`/`source_id` field naming inconsistency in returned dicts. `related_domains` as JSON-in-TEXT is pragmatic. |
| Testing | 6 | Curation, basic server, and chunking/BM25/facts/validator unit tests now in place. Still missing: end-to-end ingest + hybrid-search integration tests behind a Qdrant-backed fixture. |

## Recent Improvements (2026-05-04)

- `knowledge_search` now returns `source_id` and `chunk_id` per result so
  agents can chain to download/rename/extract without a second
  `knowledge_sources` lookup.
- Fact-keyword extraction uses `re.findall(r"\b\w{2,}\b", q.lower())` instead
  of `split()` — keeps short identifiers (`vw`, `ldl`, `hr`) and strips
  trailing punctuation.
- `facts_search` matches both `key` and `value` columns. Queries like
  "blood pressure 130" now hit a fact whose value is `130/82`.
- `knowledge_search` accepts optional `max_chars` to truncate chunk content
  for context-size control. Default `None` preserves prior behavior.
- `GET /api/search` now uses the same shared search helper as
  `knowledge_search`: related-domain/core resolution, `source_id`/`chunk_id`
  result metadata, `max_chars`, and regex-based fact keywords stay in sync.
- `knowledge_sources` now reports `download_error` instead of silently
  dropping the URL when token creation fails.
- Added `tests/test_knowledge_internals.py` covering
  `chunk_text`, `BM25SparseEncoder`, `_is_likely_binary`, the text-ingest
  validator, search keyword extraction, and `facts_search` key/value
  semantics.
- Replaced `print(..., file=sys.stderr)` with stdlib `logging` across
  `knowledge.py` and `knowledge_api.py`. New `shared/logging_config.py`
  centralizes setup; `LOG_LEVEL` env var controls verbosity.
- Wrapped every `@mcp.tool` on the knowledge server with `@logged_tool(log)`,
  emitting per-call `tool=... status=... duration_ms=...` lines.
- Added `GET /api/health` endpoint reporting Qdrant reachability, source
  count, chunk count, BM25 doc count, and embedding model.
- Added `tests/test_logging_config.py` (10 tests) covering the decorator's
  success / error / duration / name-preservation paths and the result
  summarizer.
- Added shared-search parity tests for REST/MCP result fields, domain
  resolution, truncation, and fact keyword extraction. Current suite: 87 tests.

## Recommended Next Investments (in order)

1. **`knowledge_doctor` reconciliation tool.** Scan Qdrant chunks vs SQLite
   sources, report orphans on either side, optionally fix. Cheap insurance
   against the dual-store consistency footgun.
2. **Tests for hybrid search and chunking math.** A small set of golden-query
   tests would catch regressions and let scoring tweaks ship confidently.
3. **Cross-encoder rerank** (e.g. `bge-reranker-base`) over the top-30 hybrid
   results, return top-10. Single biggest quality lever short of switching
   embedding models.

## Deferred / Considered-Not-Worth-It

- BM25 stats persistence. Already warmed up on startup from `chunks_all()` —
  not a real bug.
- Per-batch IDF drift on sparse vectors. Real but minor; RRF is rank-based.
- `download_token_get` cleanup-on-every-call. Micro-cost, single-user system.
- Smart snippet extraction around match. Adds complexity for unclear win;
  the simpler `max_chars` cap covers the cost concern.
