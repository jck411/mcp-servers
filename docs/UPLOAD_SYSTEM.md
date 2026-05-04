# Knowledge Upload System

Everything that happens when you drop a file into the upload UI at `https://api-knowledge.jackshome.com/ui/`.

---

## Components

| Component | File | Port |
|-----------|------|------|
| Upload UI | `web/upload.html` | served from knowledge_api |
| REST API | `servers/knowledge_api.py` | 9018 |
| Core pipeline | `servers/knowledge.py` | (imported by knowledge_api) |
| Vector store | Qdrant at `192.168.1.110:6333` | 6333 |
| Metadata DB | SQLite at `data/knowledge.db` | — |
| File storage | `knowledge/<domain>/<filename>` | — |

---

## Upload Flow

```
Browser → POST /api/upload/{domain}?overwrite=&force=
             │
             ├─ 409 if file exists and overwrite=false
             ├─ write bytes to knowledge/<domain>/<filename>
             └─ _ingest_file_at_path()
                    │
                    ├─ hash file (SHA-256)
                    ├─ check DB for existing hash
                    │    ├─ duplicate + no stored_path → backfill path, return BACKFILLED
                    │    └─ exact duplicate with bytes → skip, return DUPLICATE
                    │
                    └─ _extract_and_chunk_with_log()  ← determines pipeline_type
                           │
                           ├─ .pdf          → document_ocr  (vision LLM per page)
                           ├─ .jpg/.png/…   → image_description  (vision LLM, single call)
                           ├─ .txt/.md/…    → text_read  (read bytes, decode UTF-8)
                           └─ other         → unsupported  (bytes stored, no chunks)
                           │
                           └─ chunking (if text was extracted)
                                  │
                                  └─ embed chunks + upsert to Qdrant
                                     insert source row in SQLite
                                     return JSON with pipeline log
```

---

## Pipeline Types

### `document_ocr` — PDFs

1. **pdf_render**: Render each page to an image at `KNOWLEDGE_VISION_DPI` (default 200 dpi), capped at `KNOWLEDGE_VISION_MAX_PAGES` (default 20).
2. **vision_ocr** (per page): Send image to `KNOWLEDGE_VISION_MODEL` (default `google/gemini-2.0-flash-001`) via OpenRouter. Returns the page text.
3. **chunking**: Split concatenated page text into overlapping chunks (`KNOWLEDGE_CHUNK_MAX_CHARS` / `KNOWLEDGE_CHUNK_OVERLAP`).
4. **embed + store**: Dense embedding via `EMBEDDING_MODEL` (default `openai/text-embedding-3-small`), sparse BM25 encoding, upsert to Qdrant.

### `image_description` — Photos/images

1. **image_description**: Send the raw image to `KNOWLEDGE_VISION_MODEL` with a description prompt. Returns a 3–5 sentence semantic description.
2. **chunking**: Treat the description as text, chunk it.
3. **embed + store**: Same as above.
   - If vision is disabled (`KNOWLEDGE_OCR_ENABLED=false`) or fails, the image is stored with `chunks_stored=0` and the Extract Facts button becomes the recommended next step.

### `text_read` — Plain text files

1. **text_read**: Read bytes, decode as UTF-8 (errors replaced), return raw text.
2. **chunking → embed + store**.

### `unsupported`

Bytes are stored on disk and a source row is created, but no chunks or embeddings. The file is downloadable. Common for `.docx`, unknown binary extensions.

---

## API Endpoints

### `POST /api/upload/{domain}`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `overwrite` | bool | false | Replace file on disk if same name exists |
| `force` | bool | false | Re-process even if content hash already ingested |

**Success response shape:**

```json
{
  "success": true,
  "file": "W2.2025.pdf",
  "domain": "finances",
  "ingested": true,
  "source_id": "5fb666ad-…",
  "chunks_stored": 15,
  "stored_path": "finances/W2.2025.pdf",
  "pipeline_type": "document_ocr",
  "pipeline": [
    {"step": "pdf_render", "status": "ok", "note": "2 pages rendered"},
    {"step": "vision_ocr", "model": "google/gemini-2.0-flash-001", "status": "ok",
     "note": "page 1/2 — 1842 chars", "tokens_in": 500, "tokens_out": 400},
    {"step": "vision_ocr", "model": "google/gemini-2.0-flash-001", "status": "ok",
     "note": "page 2/2 — 1210 chars", "tokens_in": 480, "tokens_out": 310},
    {"step": "chunking", "status": "ok", "note": "15 chunk(s) from 3052 chars"}
  ],
  "needs_extraction": false
}
```

`needs_extraction: true` is set when a warn-level step occurred during ingestion (e.g. a page had low-confidence OCR).

**Conflict (409):**

```json
{
  "detail": {
    "message": "File 'W2.2025.pdf' already exists in 'finances'. Use ?overwrite=true to replace.",
    "source_id": "5fb666ad-…",
    "source": { … }
  }
}
```

---

### `POST /api/sources/{source_id}/extract`

Runs single-shot fact extraction using `KNOWLEDGE_EXTRACTION_MODEL` (default `anthropic/claude-sonnet-4-6`).

Uses **forced tool calling** (`tool_choice: store_extracted_facts`) to guarantee structured JSON output — Claude ignores `response_format: json_object` and returns markdown otherwise.

**How it gathers content:**

- If the source is an image (`chunks=0` or image extension): reads the raw file bytes, sends as a base64 `image_url` message.
- Otherwise: loads all stored chunk text from Qdrant, concatenates, sends as a single text message.

**Response:**

```json
{
  "success": true,
  "facts_written": 8,
  "facts": {
    "document_type": "W-2 Wage and Tax Statement",
    "tax_year": "2025",
    "employer_name": "ORLANDO HEALTH",
    "box_1_wages": "130427.22"
  },
  "caption": null,
  "pipeline": [
    {"step": "load_chunks", "status": "ok", "note": "15 chunks, 27485 chars total"},
    {"step": "extraction_llm", "status": "ok", "note": "tool_call, 1648 chars",
     "tokens_in": 5200, "tokens_out": 312},
    {"step": "parse_json", "status": "ok", "note": "8 fact(s), caption=no"},
    {"step": "write_facts", "status": "ok", "note": "8 fact(s) written to 'finances'"}
  ]
}
```

Facts are stored in the domain's fact table (queryable via `GET /api/facts/{domain}`).

---

### `DELETE /api/sources/{source_id}`

Deletes source from SQLite, removes its chunks from Qdrant, and deletes the file from disk.

---

### `GET /api/sources/{domain}`

Lists all sources in a domain. Each item includes `id`, `filename`, `chunk_count`, `stored_path`, `media_type`, `size_bytes`, `ingested_at`.

---

## UI Behaviour

`web/upload.html` is served at `/ui/upload.html`.

**Upload card states:**

| Badge | Meaning |
|-------|---------|
| `UPLOADING` | In-flight — placeholder shown |
| `INDEXED` | Chunks stored, embeddings done |
| `INDEXED·REVIEW` | Indexed but `needs_extraction=true` — warn-level pipeline step |
| `DESCRIBED` | Image successfully described by vision LLM |
| `STORED` | Bytes saved, zero chunks (unsupported type) |
| `STORED·NO DESC` | Image stored but description failed |
| `BACKFILLED` | Duplicate hash; stored path was attached to existing source |
| `DUPLICATE` | Exact duplicate already fully ingested |
| `EXISTS` | Same filename on disk, `overwrite=false` (409) |
| `ERROR` | Upload failed |

**Buttons on each card:**

- **▶ show pipeline log** — expands per-step details (step name, model, token counts, notes). Only shown when `pipeline` array is non-empty.
- **⚡ Extract Facts** — calls `POST /api/sources/{id}/extract`. Live "calling model…" placeholder shown while in-flight. On success, shows fact count and rerenders pipeline log with extraction steps. On failure, shows error inline.
- **🗑 Remove** — calls `DELETE /api/sources/{id}` after a confirmation dialog. Card fades and removes itself on success.
- **copy id** — copies `source_id` to clipboard.

---

## Configuration (`.env`)

```env
OPENROUTER_API_KEY=sk-or-v1-…

# Storage
KNOWLEDGE_PATH=/opt/mcp-servers/knowledge   # default: <repo>/knowledge

# Embedding
EMBEDDING_MODEL=openai/text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

# Qdrant
QDRANT_URL=http://127.0.0.1:6333
KNOWLEDGE_QDRANT_COLLECTION=knowledge

# SQLite
KNOWLEDGE_DB_PATH=/opt/mcp-servers/data/knowledge.db

# Chunking
KNOWLEDGE_CHUNK_MAX_CHARS=1000
KNOWLEDGE_CHUNK_OVERLAP=200

# Vision / OCR
KNOWLEDGE_OCR_ENABLED=true
KNOWLEDGE_VISION_MODEL=google/gemini-2.0-flash-001
KNOWLEDGE_VISION_MAX_PAGES=20
KNOWLEDGE_VISION_DPI=200

# Extraction
KNOWLEDGE_EXTRACTION_MODEL=anthropic/claude-sonnet-4-6
```

---

## Filesystem Permissions

The systemd service (`mcp-server@.service`) runs with `ProtectSystem=strict`. These paths must be in `ReadWritePaths`:

```
/opt/mcp-servers/credentials
/opt/mcp-servers/logs
/opt/mcp-servers/data        ← SQLite DB lives here
/opt/mcp-servers/knowledge   ← uploaded files live here
```

Missing `knowledge` from this list causes `OSError: [Errno 30] Read-only file system` on every upload.

---

## Deployment

### How `deploy.sh` works

`./deploy/deploy.sh [--no-push] [server …]` auto-detects which path to use:

1. **Local** — tries `ssh root@192.168.1.110` (3 s timeout). Succeeds when on the home LAN.
2. **Tunnel** — tries `ssh proxmox-tunnel` (Cloudflare tunnel, 8 s timeout). Works from anywhere with internet.
3. **Remote/console** — both SSH paths unreachable; prints `pct exec` commands to paste into the Proxmox web console manually.

### Deploying from home (local LAN)

You are talking directly to LXC 110 at `192.168.1.110`. This is the fastest path.

```bash
# Deploy one server
./deploy/deploy.sh knowledge_api

# Deploy multiple
./deploy/deploy.sh knowledge knowledge_api

# Skip the git commit/push step (code already pushed)
./deploy/deploy.sh --no-push knowledge_api

# Force local mode explicitly
./deploy/deploy.sh --local knowledge_api
```

What it does:
1. Commits any uncommitted local changes and `git push origin master`
2. `ssh root@192.168.1.110` → `git pull` + `uv sync --extra all`
3. Writes `.env.<server>` port file, kills any orphan on that port (`fuser -k`)
4. `systemctl restart mcp-server@<server>`, polls until active
5. Hits LXC 111 to refresh backend discovery

### Deploying from remote (away from home)

The `ssh proxmox-tunnel` alias must be configured in `~/.ssh/config` (Cloudflare Access tunnel). Same commands, no extra flags needed — auto-detect picks the tunnel automatically.

```bash
./deploy/deploy.sh knowledge_api
```

Internally it runs: `ssh proxmox-tunnel 'pct exec 110 -- bash -c "…"'`

### When both SSH paths fail (Proxmox console)

```bash
./deploy/deploy.sh --remote knowledge_api
```

Prints the three `pct exec` commands to paste into the Proxmox web console at `https://proxmox.jackshome.com`.

### Check service status

```bash
./deploy/deploy.sh --status
# or for a specific server:
./deploy/deploy.sh --status knowledge_api
```

### Deploy the systemd unit file itself

The service template (`deploy/mcp-server@.service`) is **not** deployed by `deploy.sh` — you must copy it manually after changing it:

```bash
# From home (local)
ssh root@192.168.1.110 'cd /opt/mcp-servers && git pull && \
  cp deploy/mcp-server@.service /etc/systemd/system/ && \
  systemctl daemon-reload && systemctl restart mcp-server@knowledge_api'

# From remote (tunnel)
ssh proxmox-tunnel 'pct exec 110 -- bash -c "
  cd /opt/mcp-servers && git pull &&
  cp deploy/mcp-server@.service /etc/systemd/system/ &&
  systemctl daemon-reload && systemctl restart mcp-server@knowledge_api
"'
```

---

## Debugging

**Upload returns 500 with plain-text body:**
```bash
ssh proxmox-tunnel 'pct exec 110 -- journalctl -u mcp-server@knowledge_api -n 50 --no-pager'
```

**Test upload directly on LXC:**
```bash
ssh proxmox-tunnel 'pct exec 110 -- bash -c "
  echo test > /tmp/t.txt &&
  curl -s -X POST http://127.0.0.1:9018/api/upload/finances?overwrite=true -F file=@/tmp/t.txt
"'
```

**Test extraction:**
```bash
ssh proxmox-tunnel 'pct exec 110 -- curl -s -X POST \
  http://127.0.0.1:9018/api/sources/<source_id>/extract \
  -H "Content-Type: application/json" -d "{}"' | python3 -m json.tool
```

**List sources in a domain:**
```bash
ssh proxmox-tunnel 'pct exec 110 -- curl -s http://127.0.0.1:9018/api/sources/finances'
```
