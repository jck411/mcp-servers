# Knowledge System

Current-state reference for `servers/knowledge_server.py`,
`servers/knowledge_admin_server.py`, and `servers/knowledge_api.py`.

## Components

| Component | File | Purpose |
|---|---|---|
| Chat MCP server | `servers/knowledge_server.py` | Domains, facts, search, wiki reads, context pack |
| Admin MCP server | `servers/knowledge_admin_server.py` | Operator cleanup, curation review, wiki admin |
| REST API | `servers/knowledge_api.py` | Facts CRUD, search, curation, health |
| SQLite | `data/knowledge.db` | Domains, facts, wiki pages, curation queue |
| Qdrant | `knowledge` collection | Dense and sparse fact + wiki vectors |

`knowledge` runs as MCP on port `9017`, `knowledge_admin` runs on port `9019`,
and `knowledge_api` runs as REST on port `9018`.

## Data Model

- **Domains** group knowledge by life area. A domain can list related domains.
- **Core** is special: searches include `core` automatically when it exists.
- **Facts** are structured key/value records stored in SQLite and keyed by
  `(domain, key)`. Each fact has a `type` classification (task, event, plan,
  preference, identity, state, reference, or note) and optional `tags` (JSON
  array) for cross-cutting labels. The `type` column enables cross-domain
  queries like "show all tasks" via `facts_by_type()`. The `tags` column
  supports sub-categorization within a domain (e.g., a yard task in the
  `home` domain has tags `["yard"]`).
- **Wiki pages** are LLM-synthesized summaries derived from facts. They are
  rebuilt by the wiki pipeline and embedded into Qdrant for semantic search.
- **Fact vectors** are derived Qdrant records with enriched text so semantic
  search can find facts by meaning instead of only exact keys.
- **Curation items** are pending approved changes. Applying destructive actions
  requires confirmation equal to the curation item id.

Note: source file storage was removed in 2026-05-26. The server no longer
stores PDFs, images, or documents. Source files live on the laptop and will
be bridged via a future local MCP tool.

## Search

`search_knowledge()` is the shared path for MCP `knowledge_search` and REST
`GET /api/search`.

1. Resolve domains:
   - Explicit `domains` wins.
   - A single `domain` expands to related domains.
   - No domain searches all non-archived domains.
   - `core` is appended when it exists.
2. Infer temporal intent as `all`, `current_upcoming`, or `historical`.
   Current/upcoming searches filter out historical and expired facts; historical
   searches include archived domains and active/archived wiki pages.
3. Expand relative year language such as "last year" before fact and vector
   retrieval.
4. Embed the expanded query and encode a sparse BM25 query.
5. Query Qdrant with hybrid dense + sparse search using RRF fusion.
6. Return fact and wiki results with similarity scores.
7. Optionally search facts by regex-extracted keywords against both fact keys
   and values. Fact results include `valid_from`, `valid_until`, `as_of`,
   `review_after`, and `temporal_status`.

Responses expose `temporal_intent`, `include_archived`, `expanded_queries`, and
fact temporal counts. Use `temporal_intent=historical` or
`temporal_intent=current_upcoming` to override inference; use `max_chars` to cap
returned content without changing stored data.

## Curation

The curation queue stores proposed actions in SQLite. Supported actions include
fact set/update/delete, archive domain, flag for review, and no-op. Source-file
actions (ingest_text, delete_source, reingest_source) are accepted but return
`skipped` since source infrastructure was removed.

Tools:

- `knowledge_curation_create`
- `knowledge_curation_list`
- `knowledge_curation_get`
- `knowledge_curation_question_packs`
- `knowledge_curation_question_pack_get`
- `knowledge_curation_pack_preview`
- `knowledge_curation_pack_apply`
- `knowledge_curation_snooze`
- `knowledge_curation_resolve`

These MCP tools live on `knowledge_admin`. The chat-facing `knowledge` MCP only
returns a pending curation count from `knowledge_context_pack`. Destructive
actions are blocked unless `confirmation` equals the item id.

The old static browser curation page was removed with the upload UI. For local
triage, use `Knowledge/knowledge-maintain.sh --check --curation-limit N`, then
inspect/apply/reject through admin MCP tools or REST.

## REST API

```text
GET    /api/health                    Liveness + dependency status
GET    /api/search?q=...             Semantic search
GET    /api/domains                  List all domains with counts
GET    /api/facts/{domain}           List facts in a domain
POST   /api/facts/{domain}/{key}     Upsert a fact
DELETE /api/facts/{domain}/{key}     Delete a fact
GET    /api/curation                 List curation queue items
POST   /api/curation                 Create/update a curation queue item
GET    /api/curation/item/{item_id}  Get one curation queue item
POST   /api/curation/apply/{item_id} Apply a reviewed curation item
POST   /api/curation/reject/{item_id} Reject a curation item
POST   /api/curation/snooze/{item_id} Snooze a curation item
```

Mutating routes require `Authorization: Bearer $KNOWLEDGE_API_TOKEN`.

## Operations

- Logging uses `shared/logging_config.py` and `LOG_LEVEL`.
- Knowledge MCP tools use `@logged_tool(log)`, emitting tool name, status,
  duration, and a short result summary.
- `GET /api/health` reports Qdrant reachability, fact count, vector count,
  BM25 document count, and embedding model.
- SQLite uses WAL, foreign keys, and `busy_timeout`.
- Qdrant indexes payload fields for domain, source id, source type, chunk
  index, and fact_type.

## Known Gaps

- Hybrid search lacks reranking, query expansion, MMR diversity, and relevance
  feedback.
- REST auth is expected to be enforced by Cloudflare Access, not app-level user
  accounts. Bearer token is an additional layer for mutating routes.
- The test suite covers internals and shared paths, but not a full Qdrant-backed
  search integration fixture.
- `mcp-server@knowledge-admin` has a naming bug (systemd template uses hyphen
  in Python module name).
