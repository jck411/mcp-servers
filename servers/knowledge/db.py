"""SQLite data layer for the Knowledge service.

Extracted from knowledge_server.py during Phase 2 modularization.
Contains KnowledgeDB (domains, facts, wiki, curation) and
search_fact_keywords (used by wiki_search).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from servers.knowledge.settings import FACT_COLUMNS, FACT_TYPES
from servers.knowledge.temporal import add_fact_temporal_status


SEARCH_STOPWORDS = frozenset({
    "about", "and", "are", "can", "current", "did", "do", "does", "for", "from", "give",
    "have", "how", "is", "last", "latest", "me", "my", "of", "on", "show", "tell",
    "the", "to", "use", "used", "was", "what", "when", "where", "which", "who",
    "with", "year",
})


def search_fact_keywords(query: str) -> list[str]:
    """Extract fact-search keywords from a free-form search query."""
    return [
        term for term in re.findall(r"\b\w{2,}\b", query.lower())
        if term not in SEARCH_STOPWORDS
    ]


class KnowledgeDB:
    """SQLite store for domains, facts, wiki pages, and curation."""

    _DOMAIN_COLUMNS = "name, description, related_domains, created_at, archived"
    _FACT_COLUMNS_QUALIFIED = (
        "f.domain, f.key, f.value, f.source, f.confidence, f.valid_from, "
        "f.valid_until, f.as_of, f.review_after, f.origin_type, f.origin_ref, "
        "f.last_confirmed_at, f.updated_at, f.type, f.tags"
    )
    _WIKI_PAGE_COLUMNS = (
        "slug, domain, title, kind, status, body_md, frontmatter_json, "
        "fact_count, source_count, created_at, updated_at"
    )
    _WIKI_PAGE_LIST_COLUMNS = (
        "slug, domain, title, kind, status, fact_count, source_count, created_at, updated_at"
    )

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    @staticmethod
    def _decode_domain_row(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "related_domains": json.loads(row["related_domains"]),
            "created_at": row["created_at"],
            "archived": bool(row["archived"]),
        }

    @staticmethod
    def _decode_fact_row(row: aiosqlite.Row) -> dict[str, Any]:
        """Decode a fact row, parsing tags JSON and adding temporal status."""
        fact = add_fact_temporal_status(dict(row))
        raw_tags = fact.get("tags")
        if raw_tags and isinstance(raw_tags, str):
            try:
                fact["tags"] = json.loads(raw_tags)
            except (json.JSONDecodeError, TypeError):
                fact["tags"] = []
        elif not raw_tags:
            fact["tags"] = []
        return fact

    @staticmethod
    def _decode_wiki_page_row(row: aiosqlite.Row) -> dict[str, Any]:
        page = dict(row)
        raw_frontmatter = str(page.pop("frontmatter_json") or "{}")
        try:
            page["frontmatter"] = json.loads(raw_frontmatter)
        except json.JSONDecodeError:
            page["frontmatter"] = {}
            page["frontmatter_json_error"] = raw_frontmatter
        return page

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS domains (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                related_domains TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL REFERENCES domains(name),
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                valid_from TEXT,
                valid_until TEXT,
                as_of TEXT,
                review_after TEXT,
                origin_type TEXT NOT NULL DEFAULT 'unknown',
                origin_ref TEXT,
                last_confirmed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_domain_key
                ON facts(domain, key);
            CREATE INDEX IF NOT EXISTS idx_facts_domain
                ON facts(domain);

            CREATE TABLE IF NOT EXISTS curation_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                risk TEXT NOT NULL DEFAULT 'medium',
                confidence REAL NOT NULL DEFAULT 0.0,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                source_refs TEXT NOT NULL DEFAULT '[]',
                proposed_actions TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_curation_status
                ON curation_items(status);
            CREATE INDEX IF NOT EXISTS idx_curation_kind
                ON curation_items(kind);

            CREATE TABLE IF NOT EXISTS wiki_pages (
                slug TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN ('entity', 'concept', 'source_summary', 'index')
                ),
                status TEXT NOT NULL DEFAULT 'candidate' CHECK (
                    status IN ('candidate', 'active', 'archived')
                ),
                body_md TEXT NOT NULL,
                frontmatter_json TEXT NOT NULL,
                fact_count INTEGER NOT NULL DEFAULT 0,
                source_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_domain
                ON wiki_pages(domain);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_kind
                ON wiki_pages(kind);

            CREATE TABLE IF NOT EXISTS wiki_page_sources (
                page_slug TEXT NOT NULL REFERENCES wiki_pages(slug) ON DELETE CASCADE,
                source_id TEXT,
                chat_date TEXT,
                contribution TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_page_sources_unique
                ON wiki_page_sources(page_slug, COALESCE(source_id, ''), COALESCE(chat_date, ''));

            CREATE TABLE IF NOT EXISTS wiki_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS wiki_rebuild_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
                scope_json TEXT NOT NULL,
                touched_slugs_json TEXT NOT NULL DEFAULT '[]',
                pages_touched INTEGER NOT NULL DEFAULT 0,
                token_estimate INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                error_summary TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_rebuild_runs_started_at
                ON wiki_rebuild_runs(started_at);

        """)
        await self._conn.executemany(
            "INSERT OR IGNORE INTO wiki_state (key, value) VALUES (?, ?)",
            (
                ("last_wiki_run", "1970-01-01T00:00:00Z"),
                ("manual_rebuild_requires_confirmation", "true"),
            ),
        )
        await self._ensure_fact_metadata_columns()
        await self._ensure_wiki_metadata_columns()
        await self._conn.commit()

    async def _ensure_fact_metadata_columns(self) -> None:
        """Add fact provenance and classification columns to older Knowledge databases."""
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(facts)")
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        additions = {
            "as_of": "TEXT",
            "review_after": "TEXT",
            "origin_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "origin_ref": "TEXT",
            "last_confirmed_at": "TEXT",
            "type": "TEXT NOT NULL DEFAULT 'note'",
            "tags": "TEXT",
        }
        for column, declaration in additions.items():
            if column not in existing:
                await self._conn.execute(
                    f"ALTER TABLE facts ADD COLUMN {column} {declaration}"  # noqa: S608
                )
        # Index for cross-domain type queries (e.g. "show all tasks")
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(type)"
        )

    async def _ensure_wiki_metadata_columns(self) -> None:
        """Add wiki lifecycle columns to older Knowledge databases."""
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(wiki_pages)")
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        if "status" not in existing:
            await self._conn.execute(
                "ALTER TABLE wiki_pages ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate' "
                "CHECK (status IN ('candidate', 'active', 'archived'))"
            )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_pages_status ON wiki_pages(status)"
        )

    # -- Domains --

    async def domain_create(
        self, name: str, description: str, related_domains: list[str]
    ) -> bool:
        """Create a domain. Returns False if it already exists."""
        assert self._conn is not None

        try:
            await self._conn.execute(
                "INSERT INTO domains (name, description, related_domains, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, description, json.dumps(related_domains), datetime.now(UTC).isoformat()),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            return False

    async def domain_list(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        cursor = await self._conn.execute(f"SELECT {self._DOMAIN_COLUMNS} FROM domains")
        rows = await cursor.fetchall()
        return [self._decode_domain_row(row) for row in rows]

    async def domain_get(self, name: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"SELECT {self._DOMAIN_COLUMNS} FROM domains WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        return self._decode_domain_row(row) if row else None

    async def domain_update_related(self, name: str, related_domains: list[str]) -> bool:
        assert self._conn is not None

        cursor = await self._conn.execute(
            "UPDATE domains SET related_domains = ? WHERE name = ?",
            (json.dumps(related_domains), name),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def domain_archive(self, name: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE domains SET archived = 1 WHERE name = ? AND archived = 0",
            (name,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def domain_exists(self, name: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT 1 FROM domains WHERE name = ?", (name,)
        )
        return await cursor.fetchone() is not None

    # -- Facts --

    async def fact_set(
        self,
        domain: str,
        key: str,
        value: str,
        source: str | None = None,
        confidence: float = 1.0,
        valid_from: str | None = None,
        valid_until: str | None = None,
        as_of: str | None = None,
        review_after: str | None = None,
        origin_type: str = "unknown",
        origin_ref: str | None = None,
        fact_type: str = "note",
        tags: list[str] | None = None,
    ) -> str:
        """Set a fact. Upserts by (domain, key). Returns fact ID.

        Args:
            fact_type: Classification of the fact — task, event, plan,
                preference, identity, state, reference, or note (default).
            tags: Optional cross-cutting labels (e.g. ["yard", "driveway"]).
                Stored as a JSON array string.
        """
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        fact_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{domain}:{key}"))

        # Validate and normalise type
        clean_type = str(fact_type or "note").strip().lower()
        if clean_type not in FACT_TYPES:
            clean_type = "note"
        tags_json = json.dumps(sorted(set(tags))) if tags else None

        await self._conn.execute(
            """
            INSERT INTO facts (id, domain, key, value, source, confidence,
                               valid_from, valid_until, as_of, review_after,
                               origin_type, origin_ref, last_confirmed_at,
                               created_at, updated_at, type, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(domain, key) DO UPDATE SET
                last_confirmed_at = CASE
                    WHEN facts.value = excluded.value THEN excluded.updated_at
                    ELSE NULL
                END,
                value = excluded.value,
                source = excluded.source,
                confidence = excluded.confidence,
                valid_from = excluded.valid_from,
                valid_until = excluded.valid_until,
                as_of = excluded.as_of,
                review_after = excluded.review_after,
                origin_type = excluded.origin_type,
                origin_ref = excluded.origin_ref,
                updated_at = excluded.updated_at,
                type = excluded.type,
                tags = excluded.tags
            """,
            (fact_id, domain, key, value, source, confidence,
             valid_from, valid_until, as_of, review_after, origin_type, origin_ref,
             now, now, clean_type, tags_json),
        )
        await self._conn.commit()
        return fact_id

    async def fact_get(self, domain: str, key: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM facts WHERE domain = ? AND key = ?", (domain, key)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def fact_delete(self, domain: str, key: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM facts WHERE domain = ? AND key = ?", (domain, key)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def facts_list(
        self,
        domain: str,
        *,
        fact_type: str | None = None,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        conditions = ["domain = ?"]
        params: list[Any] = [domain]
        if fact_type:
            conditions.append("type = ?")
            params.append(fact_type)
        where = " AND ".join(conditions)
        cursor = await self._conn.execute(
            f"SELECT {FACT_COLUMNS} FROM facts WHERE {where} ORDER BY key",  # noqa: S608
            params,
        )
        rows = await cursor.fetchall()
        return [self._decode_fact_row(row) for row in rows]

    async def facts_by_type(
        self,
        fact_type: str,
        *,
        domains: list[str] | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Return all facts of a given type across all (or specified) domains.

        Useful for cross-domain queries like "show all my tasks".
        """
        assert self._conn is not None
        conditions = ["f.type = ?"]
        params: list[Any] = [fact_type]
        if domains:
            placeholders = ",".join("?" for _ in domains)
            conditions.append(f"f.domain IN ({placeholders})")
            params.extend(domains)
        if not include_archived:
            conditions.append(
                "(d.archived = 0 OR d.archived IS NULL)"
            )
        where = " AND ".join(conditions)
        cursor = await self._conn.execute(
            f"""
            SELECT {self._FACT_COLUMNS_QUALIFIED}
            FROM facts f
            LEFT JOIN domains d ON f.domain = d.name
            WHERE {where}
            ORDER BY f.domain, f.key
            """,  # noqa: S608
            params,
        )
        rows = await cursor.fetchall()
        return [self._decode_fact_row(row) for row in rows]

    async def facts_search(self, domains: list[str], keys: list[str]) -> list[dict[str, Any]]:
        """Search facts across multiple domains by substring match on key OR value."""
        assert self._conn is not None
        if not domains:
            return []
        placeholders_d = ",".join("?" for _ in domains)
        conditions = [f"domain IN ({placeholders_d})"]
        params: list[Any] = list(domains)

        if keys:
            term_conditions = []
            for k in keys:
                term_conditions.append("(key LIKE ? OR value LIKE ?)")
                params.append(f"%{k}%")
                params.append(f"%{k}%")
            conditions.append(f"({' OR '.join(term_conditions)})")

        where = " AND ".join(conditions)
        cursor = await self._conn.execute(
            f"SELECT {FACT_COLUMNS} FROM facts WHERE {where} ORDER BY domain, key",  # noqa: S608
            params,
        )
        rows = await cursor.fetchall()
        return [self._decode_fact_row(row) for row in rows]

    # -- Wiki Pages --

    async def wiki_get(self, slug: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"SELECT {self._WIKI_PAGE_COLUMNS} FROM wiki_pages WHERE slug = ?",  # noqa: S608
            (slug,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        page = self._decode_wiki_page_row(row)
        cursor = await self._conn.execute(
            """
            SELECT source_id, chat_date, contribution
            FROM wiki_page_sources
            WHERE page_slug = ?
            ORDER BY COALESCE(source_id, ''), COALESCE(chat_date, ''), contribution
            """,
            (slug,),
        )
        page["sources"] = [dict(source) for source in await cursor.fetchall()]
        return page

    async def wiki_list(
        self,
        *,
        domain: str | None = None,
        kind: str | None = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        limit = max(1, min(int(limit or 50), 200))
        conditions: list[str] = []
        params: list[Any] = []
        if domain:
            conditions.append("domain = ?")
            params.append(domain)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if status != "all":
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._conn.execute(
            f"""
            SELECT {self._WIKI_PAGE_LIST_COLUMNS}
            FROM wiki_pages
            {where}
            ORDER BY updated_at DESC, slug
            LIMIT ?
            """,  # noqa: S608
            [*params, limit],
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def wiki_search(
        self,
        domains: list[str],
        query: str,
        *,
        limit: int = 5,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search wiki pages with a lightweight local ranker."""
        assert self._conn is not None
        terms = search_fact_keywords(query)
        if not terms:
            return []
        page_statuses = statuses or {"active"}
        placeholders_s = ",".join("?" for _ in page_statuses)
        conditions = [f"status IN ({placeholders_s})"]
        params: list[Any] = sorted(page_statuses)
        if domains:
            placeholders = ",".join("?" for _ in domains)
            conditions.append(f"domain IN ({placeholders})")
            params.extend(domains)

        cursor = await self._conn.execute(
            f"""
            SELECT {self._WIKI_PAGE_COLUMNS}
            FROM wiki_pages
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC
            LIMIT 500
            """,  # noqa: S608
            params,
        )
        ranked = []
        phrase = query.lower().strip()
        for row in await cursor.fetchall():
            page = self._decode_wiki_page_row(row)
            frontmatter = page.get("frontmatter") or {}
            aliases = " ".join(str(a) for a in frontmatter.get("aliases") or [])
            title_text = f"{page['slug']} {page['title']} {aliases}".lower()
            body_text = str(page.get("body_md") or "").lower()
            score = 0
            if phrase and phrase in title_text:
                score += 30
            if phrase and phrase in body_text:
                score += 6
            for term in terms:
                if term in title_text:
                    score += 10
                if term in body_text:
                    score += 1
            if page["kind"] == "index":
                score -= 3
            if score > 0:
                page["score"] = score
                ranked.append(page)
        ranked.sort(key=lambda page: (-int(page["score"]), str(page["slug"])))
        return ranked[:max(1, min(limit, 20))]

    async def wiki_set_status(self, slug: str, status: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE wiki_pages SET status = ?, updated_at = ? WHERE slug = ?",
            (status, datetime.now(UTC).isoformat(), slug),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def wiki_upsert_page(
        self,
        *,
        slug: str,
        domain: str,
        title: str,
        kind: str,
        status: str,
        body_md: str,
        frontmatter: dict[str, Any],
        sources: list[dict[str, Any]],
        fact_count: int,
    ) -> None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT INTO wiki_pages
                (slug, domain, title, kind, status, body_md, frontmatter_json,
                 fact_count, source_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                domain = excluded.domain,
                title = excluded.title,
                kind = excluded.kind,
                status = excluded.status,
                body_md = excluded.body_md,
                frontmatter_json = excluded.frontmatter_json,
                fact_count = excluded.fact_count,
                source_count = excluded.source_count,
                updated_at = excluded.updated_at
            """,
            (
                slug, domain, title, kind, status, body_md,
                json.dumps(frontmatter, sort_keys=True), fact_count, len(sources), now, now,
            ),
        )
        await self._conn.execute("DELETE FROM wiki_page_sources WHERE page_slug = ?", (slug,))
        seen: set[tuple[str, str]] = set()
        rows = []
        for source in sources:
            source_id = str(source.get("source_id") or "").strip() or None
            chat_date = str(source.get("chat_date") or "").strip() or None
            key = (source_id or "", chat_date or "")
            if not any(key) or key in seen:
                continue
            seen.add(key)
            rows.append((
                slug,
                source_id,
                chat_date,
                str(source.get("contribution") or "cited evidence").strip()[:200],
            ))
        await self._conn.executemany(
            """
            INSERT OR IGNORE INTO wiki_page_sources
                (page_slug, source_id, chat_date, contribution)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        await self._conn.commit()

    async def wiki_state_get(self, key: str, default: str | None = None) -> str | None:
        assert self._conn is not None
        cursor = await self._conn.execute("SELECT value FROM wiki_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return str(row["value"]) if row else default

    async def wiki_state_set(self, key: str, value: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO wiki_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    async def wiki_rebuild_run_start(
        self, *, scope: dict[str, Any], token_estimate: int, model: str | None
    ) -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO wiki_rebuild_runs (status, scope_json, token_estimate, model)
            VALUES ('running', ?, ?, ?)
            """,
            (json.dumps(scope, sort_keys=True), token_estimate, model),
        )
        await self._conn.commit()
        return int(cursor.lastrowid)

    async def wiki_rebuild_run_finish(
        self,
        run_id: int,
        *,
        status: str,
        touched_slugs: list[str],
        token_estimate: int,
        error_summary: str | None = None,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE wiki_rebuild_runs
            SET finished_at = ?, status = ?, touched_slugs_json = ?,
                pages_touched = ?, token_estimate = ?, error_summary = ?
            WHERE id = ?
            """,
            (
                datetime.now(UTC).isoformat(), status, json.dumps(touched_slugs),
                len(touched_slugs), token_estimate, error_summary, run_id,
            ),
        )
        await self._conn.commit()

    async def wiki_rebuild_inputs(
        self,
        *,
        since: str,
        domain: str | None = None,
        force_full: bool = False,
        quiet_after: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        assert self._conn is not None
        fact_conditions: list[str] = []
        fact_params: list[Any] = []
        if domain:
            fact_conditions.append("domain = ?")
            fact_params.append(domain)
        if not force_full:
            fact_conditions.append("updated_at > ?")
            fact_params.append(since)
        if quiet_after:
            fact_conditions.append(
                "NOT (origin_type = 'chat' AND created_at > ? AND last_confirmed_at IS NULL)"
            )
            fact_params.append(quiet_after)

        fact_where = f"WHERE {' AND '.join(fact_conditions)}" if fact_conditions else ""
        cursor = await self._conn.execute(
            f"""
            SELECT domain, key, origin_type, origin_ref, last_confirmed_at, created_at, updated_at
            FROM facts
            {fact_where}
            ORDER BY domain, key
            """,  # noqa: S608
            fact_params,
        )
        facts = [dict(row) for row in await cursor.fetchall()]
        return {"facts": facts, "sources": []}

    # -- Curation Queue --

    @staticmethod
    def _decode_curation_row(row: aiosqlite.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("source_refs", "proposed_actions"):
            try:
                item[key] = json.loads(item[key] or "[]")
            except json.JSONDecodeError:
                item[key] = []
        return item

    async def curation_upsert(
        self,
        *,
        kind: str,
        title: str,
        summary: str = "",
        source_refs: list[dict[str, Any]] | None = None,
        proposed_actions: list[dict[str, Any]] | None = None,
        risk: str = "medium",
        confidence: float = 0.0,
        item_id: str | None = None,
        status: str = "pending",
        created_at: str | None = None,
    ) -> str:
        """Create or replace a curation queue item."""
        assert self._conn is not None
        curation_id = item_id or str(uuid.uuid4())
        now = created_at or datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT INTO curation_items
                (id, kind, status, risk, confidence, title, summary, source_refs,
                 proposed_actions, created_at, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                status = excluded.status,
                risk = excluded.risk,
                confidence = excluded.confidence,
                title = excluded.title,
                summary = excluded.summary,
                source_refs = excluded.source_refs,
                proposed_actions = excluded.proposed_actions,
                created_at = excluded.created_at,
                reviewed_at = CASE
                    WHEN excluded.status = 'pending' THEN NULL
                    ELSE curation_items.reviewed_at
                END
            WHERE curation_items.status = 'pending' OR excluded.status != 'pending'
            """,
            (
                curation_id,
                kind,
                status,
                risk,
                confidence,
                title,
                summary,
                json.dumps(source_refs or []),
                json.dumps(proposed_actions or []),
                now,
            ),
        )
        await self._conn.commit()
        return curation_id

    async def curation_get(self, item_id: str) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM curation_items WHERE id = ?",
            (item_id,),
        )
        row = await cursor.fetchone()
        return self._decode_curation_row(row) if row else None

    async def curation_list(
        self,
        *,
        status: str | None = "pending",
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        limit = max(1, min(limit, 200))
        conditions = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._conn.execute(
            f"SELECT * FROM curation_items {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        )
        rows = await cursor.fetchall()
        return [self._decode_curation_row(row) for row in rows]

    async def curation_count(
        self,
        *,
        status: str | None = "pending",
        kind: str | None = None,
    ) -> int:
        assert self._conn is not None
        conditions = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._conn.execute(
            f"SELECT COUNT(*) FROM curation_items {where}",  # noqa: S608
            params,
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def curation_mark_status(self, item_id: str, status: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE curation_items SET status = ?, reviewed_at = ? WHERE id = ?",
            (status, datetime.now(UTC).isoformat(), item_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
