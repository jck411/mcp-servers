# Knowledge System

Current-state reference for `servers/knowledge.py`, `servers/knowledge_api.py`,
and the upload UI.

## Components

| Component | File | Purpose |
|---|---|---|
| MCP server | `servers/knowledge.py` | Domains, facts, source ingest, search, curation tools |
| REST API | `servers/knowledge_api.py` | Upload UI backend, source CRUD, search, health |
| Upload UI | `web/upload.html` | Browser workflow for upload, delete, and extraction |
| SQLite | `data/knowledge.db` | Domains, facts, source metadata, curation queue, download tokens |
| Qdrant | `knowledge` collection | Dense and sparse chunk vectors |
| Raw files | `knowledge/<domain>/<filename>` | Uploaded source bytes |

`knowledge` runs as MCP on port `9017`. `knowledge_api` runs as REST on port
`9018`.

## Data Model

- **Domains** group knowledge by subject. A domain can list related domains.
- **Core** is special: searches include `core` automatically when it exists.
- **Facts** are structured key/value records stored in SQLite and keyed by
  `(domain, key)`.
- **Sources** track uploaded or text-ingested material: domain, filename,
  content hash, stored path, media type, size, chunk count, and ingest time.
- **Chunks** live in Qdrant with payload fields such as `source_id`,
  `source_name`, `domain`, `chunk_index`, and `content`.
- **Curation items** are pending approved changes. Applying destructive actions
  requires confirmation equal to the curation item id.

## Ingestion

All file upload paths converge on `_ingest_file_at_path()`:

1. Hash the file with SHA-256.
2. Check SQLite for an existing source with the same hash.
3. Backfill missing stored-file metadata for older rows, or skip exact
   duplicates.
4. Extract text:
   - PDFs: native text first, then vision OCR when needed.
   - Images: vision description.
   - Text files: UTF-8 decode.
   - Unsupported binaries: store bytes only, with zero chunks.
5. Chunk extracted text with `KNOWLEDGE_CHUNK_MAX_CHARS` and
   `KNOWLEDGE_CHUNK_OVERLAP`.
6. Generate dense embeddings through OpenRouter.
7. Generate sparse BM25 vectors locally.
8. Upsert chunks to Qdrant and write the source row to SQLite.

Text-only MCP ingest uses `knowledge_ingest_text`; binary-looking names and
source types are rejected so agents do not create fake `.pdf` rows without
stored bytes.

## Search

`search_knowledge()` is the shared path for MCP `knowledge_search` and REST
`GET /api/search`.

1. Resolve domains:
   - Explicit `domains` wins.
   - A single `domain` expands to related domains.
   - No domain searches all non-archived domains.
   - `core` is appended when it exists.
2. Embed the query and encode a sparse BM25 query.
3. Query Qdrant with hybrid dense + sparse search using RRF fusion.
4. Return chunk results with `source_id`, `chunk_id`, `source_name`,
   `source_type`, `chunk_index`, and similarity.
5. Optionally search facts by regex-extracted keywords against both fact keys
   and values. Fact results include `valid_from`, `valid_until`, `as_of`,
   `review_after`, and `temporal_status`.

Use `max_chars` to cap returned chunk content without changing stored data.

## Source Management

- Downloads resolve raw bytes from `stored_path`; if the file is missing and
  vectors are available, the system can export stored chunks as Markdown.
- Download URLs use short-lived SQLite tokens.
- Deleting a source removes its Qdrant chunks, SQLite row, and raw file unless
  another source still references the same file.
- Renaming updates SQLite, Qdrant payload `source_name`, and the raw file when
  it is safe to move.
- Upload conflicts are handled as delete-then-upload in the UI. Direct API
  callers can use `overwrite` or `force`.

## Fact Extraction

`POST /api/sources/{source_id}/extract` and the MCP extraction path call
`extract_source_facts_single_shot()`.

- Images are sent to the extraction model as base64 image content.
- Indexed text sources load their Qdrant chunks and send one combined text
  prompt.
- Unsupported zero-chunk binaries are rejected.
- The OpenRouter chat call forces the `store_extracted_facts` tool so the model
  returns structured facts plus an optional caption.
- Extracted facts are upserted into SQLite. Captions are embedded as searchable
  chunks linked to the original source.

## Curation

The curation queue stores proposed actions in SQLite. Supported actions include
fact set/update/delete, ingest text, delete source, archive domain, flag for
review, and no-op.

Tools:

- `knowledge_curation_create`
- `knowledge_curation_list`
- `knowledge_curation_get`
- `knowledge_curation_apply`
- `knowledge_curation_reject`
- `knowledge_curation_snooze`

Destructive actions are blocked unless `confirmation` equals the item id.

## Operations

- Logging uses `shared/logging_config.py` and `LOG_LEVEL`.
- Knowledge MCP tools use `@logged_tool(log)`, emitting tool name, status,
  duration, and a short result summary.
- `GET /api/health` reports Qdrant reachability, source count, chunk count,
  BM25 document count, and embedding model.
- SQLite uses WAL, foreign keys, and `busy_timeout`.
- Qdrant indexes payload fields for domain, source id, source type, and chunk
  index.

## Known Gaps

- No reconciliation tool for Qdrant chunks versus SQLite source rows.
- No transaction boundary across file writes, SQLite writes, embedding calls,
  and Qdrant upserts.
- Hybrid search lacks reranking, query expansion, MMR diversity, and relevance
  feedback.
- REST auth is expected to be enforced by Cloudflare Access, not app-level user
  accounts.
- The test suite covers internals and shared paths, but not a full Qdrant-backed
  ingest/search integration fixture.
