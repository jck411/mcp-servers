# Memory Tools — User Guide

The housekeeping MCP server includes semantic long-term memory tools that persist information across conversations. These tools use vector embeddings for similarity search, so you can recall memories using natural language.

## Profile-Based Isolation

Memory tools are created **per-profile**, allowing complete isolation between users. The profiles are hardcoded in `shared/memory_config.py`:

```python
memory_profiles: list[str] = Field(
    default_factory=lambda: ["jack", "family"],
)
```

This creates separate tool sets:
- `remember_jack`, `recall_jack`, `forget_jack`, `reflect_jack`, `memory_stats_jack`
- `remember_family`, `recall_family`, `forget_family`, `reflect_family`, `memory_stats_family`

Each profile's memories are stored with a unique `user_id` — Jack's memories are completely isolated from Family memories.

### Backend Integration

Configure your backend to enable only the relevant profile's tools per conversation:

```json
{
  "profile": "jack",
  "disabled_tools": [
    "remember_family", "recall_family", "forget_family", 
    "reflect_family", "memory_stats_family"
  ]
}
```

Or inversely, disable Jack's tools for Family conversations.

## How It Works

Memory tools are **passive** — the LLM must explicitly call them. They don't automatically capture or retrieve anything. You configure the LLM's behavior via system prompt.

### Architecture

```
User Message
     ↓
LLM decides to call `remember` or `recall`
     ↓
housekeeping server
     ├─ OpenRouter API (embeddings)
     ├─ Qdrant (vector search)
     └─ SQLite (metadata, access tracking)
```

## Tools

### `remember` — Store a memory

Store facts, preferences, corrections, or summaries for later retrieval.

```json
{
  "content": "User prefers dark mode",
  "category": "preference",
  "tags": ["ui", "settings"],
  "importance": 0.7,
  "pinned": false,
  "ttl_hours": null
}
```

**Parameters:**
- `content` (required): The information to store
- `category`: `fact`, `preference`, `summary`, `instruction`, `episode` (default: `fact`)
- `tags`: Optional list for filtering
- `importance`: 0.0–1.0 (higher = harder to forget during cleanup)
- `session_id`: Tie to a conversation session
- `pinned`: If true, never auto-deleted
- `ttl_hours`: Auto-expire after N hours

### `recall` — Search memories

Semantic search over stored memories using natural language.

```json
{
  "query": "user interface preferences",
  "category": "preference",
  "limit": 5,
  "min_similarity": 0.5
}
```

**Parameters:**
- `query` (required): Natural language search
- `category`: Filter to specific category
- `tags`: Filter to memories with these tags
- `session_id`: Limit to a session
- `time_range_hours`: Only recent memories
- `limit`: Max results (default: 10)
- `min_similarity`: Cosine threshold 0.0–1.0 (default: 0.4)

**Returns:** List of memories ranked by similarity, with scores.

### `forget` — Delete memories

Remove memories by ID, session, or filters.

```json
{
  "memory_id": "uuid-here"
}
```

Or bulk delete:
```json
{
  "session_id": "conv-123",
  "include_pinned": false
}
```

### `reflect` — Store session summary

Create a high-importance episode memory summarizing a conversation.

```json
{
  "session_id": "conv-123",
  "summary": "Discussed home automation. User wants motion-activated lights in hallway."
}
```

This creates a pinned `episode` memory that won't be auto-deleted.

### `memory_stats` — View statistics

Returns total count, categories breakdown, oldest/newest timestamps.

---

## System Prompt Addition

Add this to your LLM's system prompt to enable memory behavior. Replace `{profile}` with the actual profile name (e.g., `jack` or `family`):

```
You have access to long-term memory that persists across conversations:

- Use `recall_{profile}` BEFORE answering questions about past interactions, user 
  preferences, or anything the user might have told you previously.
- Use `remember_{profile}` to store important facts, user preferences, corrections, 
  or instructions the user gives you. Be selective — don't store trivial 
  conversation details.
- Use `reflect_{profile}` at the end of meaningful conversations to save a summary.
- Use `forget_{profile}` when the user explicitly asks you to forget something.

Memory categories: fact, preference, summary, instruction, episode.
Memories marked as pinned persist permanently.

When recalling, use natural language queries that match what you're looking for.
Example: recall_{profile}("user's timezone preference") rather than recall_{profile}("timezone").
```

---

## Automatic Maintenance

The server runs background tasks:

| Task | Interval | What it does |
|------|----------|--------------|
| Cleanup | 15 minutes | Removes expired (past TTL) and stale (low importance, never accessed) memories |
| Decay | 24 hours | Reduces importance of unpinned memories older than 7 days by 5% |

This means low-importance, unused memories gradually fade away like real memory.

---

## Configuration

Environment variables (set in `/opt/mcp-servers/.env` on LXC):

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key for embeddings |
| `EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Embedding model |
| `EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions |
| `QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant server URL |
| `QDRANT_COLLECTION` | `memories` | Collection name |
| `MEMORY_DB_PATH` | `data/memory.db` | SQLite metadata path |

### Adding a New Profile

To add a new profile, edit `shared/memory_config.py`:

```python
memory_profiles: list[str] = Field(
    default_factory=lambda: ["jack", "family", "guest"],
)
```

Then redeploy:
```bash
./refresh.sh housekeeping
```

---

## Graceful Degradation

If Qdrant is unreachable or `OPENROUTER_API_KEY` is missing, the memory subsystem disables itself and logs a warning. The other housekeeping tools (`current_time`, `test_echo`) continue to work.

---

## Example Conversation

**Day 1 (Jack's conversation):**
```
User: I'm allergic to peanuts
LLM: [calls remember_jack(content="User has peanut allergy", category="fact", importance=0.9)]
     I've noted that. I'll keep it in mind for any food-related suggestions.
```

**Day 30 (Jack's conversation):**
```
User: Can you suggest a Thai restaurant?
LLM: [calls recall_jack(query="user food allergies dietary restrictions")]
     → Returns: "User has peanut allergy" (similarity: 0.82)
     
     Sure! When looking at Thai restaurants, I'll watch out for dishes with 
     peanuts since you mentioned your allergy. Here are some options...
```

**Family conversation (same day):**
```
User: What allergies do we have?
LLM: [calls recall_family(query="allergies")]
     → Returns: No matching memories found.
     
     I don't have any allergy information stored for the family. Would you 
     like to add any?
```

Note: Jack's personal allergy information is isolated from the Family memory space.

---

## Troubleshooting

**Memory subsystem disabled:**
```
[HOUSEKEEPING] Memory subsystem disabled — config error: ...
[HOUSEKEEPING] Memory subsystem disabled — Qdrant unreachable: ...
```

Check:
1. `OPENROUTER_API_KEY` is set in `.env`
2. Qdrant is running: `systemctl status qdrant`
3. Qdrant is accessible: `curl http://127.0.0.1:6333/healthz`

**No memories returned:**
- Try lowering `min_similarity` (e.g., 0.3)
- Check `memory_stats` to see if any memories exist
- Verify the query is semantically related to stored content
