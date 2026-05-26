"""Web search MCP server using Serper (Google Search) + Firecrawl + Jina Reranker.

Mirrors the search stack from LibreChat's native web search:
  - Serper.dev  → Google Search results (organic, news, images)
  - Firecrawl   → JavaScript-rendered page scraping to clean markdown
  - Jina AI     → Reranker for result quality (optional, graceful fallback)

Environment variables:
  SERPER_API_KEY    — required for search
  FIRECRAWL_API_KEY — required for page scraping (falls back to httpx+trafilatura)
  FIRECRAWL_API_URL — optional, default https://api.firecrawl.dev
  JINA_API_KEY      — optional, enables reranking
  JINA_API_URL      — optional, default https://api.jina.ai

Run:
    python -m servers.web_search --transport streamable-http --host 0.0.0.0 --port 9016
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 9016
SEARCH_TIMEOUT = 15  # seconds per search request
FETCH_TIMEOUT = 20  # seconds per page fetch
MAX_SEARCH_RESULTS = 10

# Serper
SERPER_API_URL = "https://google.serper.dev"
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

# Firecrawl
FIRECRAWL_API_URL = os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev").rstrip("/")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")

# Jina reranker
JINA_API_URL = os.getenv("JINA_API_URL", "https://api.jina.ai").rstrip("/")
JINA_API_KEY = os.getenv("JINA_API_KEY", "")
JINA_RERANKER_MODEL = os.getenv("JINA_RERANKER_MODEL", "jina-reranker-v2-base-multilingual")

# Concurrency limiter
_search_semaphore = asyncio.Semaphore(int(os.getenv("WEB_SEARCH_CONCURRENCY", "5")))

mcp = FastMCP("web_search")

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True)
    return _http_client


# ---------------------------------------------------------------------------
# Serper search helpers
# ---------------------------------------------------------------------------


async def _serper_request(endpoint: str, payload: dict) -> dict:
    """Make a POST request to Serper API."""
    if not SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY not configured")
    client = await _get_client()
    resp = await client.post(
        f"{SERPER_API_URL}/{endpoint}",
        json=payload,
        headers={
            "X-API-KEY": SERPER_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _format_organic_result(result: dict, index: int) -> str:
    """Format a single organic search result."""
    title = result.get("title", "No title")
    snippet = result.get("snippet", "")
    link = result.get("link", "")
    date = result.get("date", "")

    parts = [f"--- Result {index} ---", f"**{title}**"]
    if date:
        parts.append(f"Date: {date}")
    if snippet:
        parts.append(snippet)
    if link:
        parts.append(f"URL: {link}")

    return "\n".join(parts)


def _format_news_result(result: dict, index: int) -> str:
    """Format a single news search result."""
    title = result.get("title", "No title")
    snippet = result.get("snippet", "")
    link = result.get("link", "")
    source = result.get("source", "")
    date = result.get("date", "")

    parts = [f"--- News {index} ---", f"**{title}**"]
    if source:
        parts.append(f"Source: {source}")
    if date:
        parts.append(f"Date: {date}")
    if snippet:
        parts.append(snippet)
    if link:
        parts.append(f"URL: {link}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Jina reranker helper
# ---------------------------------------------------------------------------


async def _rerank_results(
    query: str,
    results: list[dict],
    text_key: str = "snippet",
    top_n: int | None = None,
) -> list[dict]:
    """Rerank results using Jina Reranker. Falls back to original order."""
    if not JINA_API_KEY or len(results) < 2:
        return results

    documents = [r.get(text_key, r.get("title", "")) for r in results]
    # Skip if all docs are empty
    if not any(documents):
        return results

    try:
        client = await _get_client()
        resp = await client.post(
            f"{JINA_API_URL}/v1/rerank",
            json={
                "model": JINA_RERANKER_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n or len(results),
            },
            headers={
                "Authorization": f"Bearer {JINA_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        ranked = data.get("results", [])
        if not ranked:
            return results

        # Rebuild results in reranked order
        reranked = []
        for item in ranked:
            idx = item.get("index", 0)
            if 0 <= idx < len(results):
                entry = dict(results[idx])
                entry["relevance_score"] = round(item.get("relevance_score", 0), 4)
                reranked.append(entry)
        return reranked if reranked else results

    except Exception:
        # Reranking is optional — silently fall back
        return results


# ---------------------------------------------------------------------------
# Firecrawl scraper helper
# ---------------------------------------------------------------------------


async def _firecrawl_scrape(url: str, max_length: int = 0) -> str:
    """Scrape a URL using Firecrawl API, return markdown content."""
    if not FIRECRAWL_API_KEY:
        raise RuntimeError("FIRECRAWL_API_KEY not configured — cannot scrape pages")

    client = await _get_client()
    payload: dict[str, Any] = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": FETCH_TIMEOUT * 1000,  # Firecrawl uses ms
    }

    resp = await client.post(
        f"{FIRECRAWL_API_URL}/v1/scrape",
        json=payload,
        headers={
            "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=FETCH_TIMEOUT + 5,  # give Firecrawl extra time
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        error = data.get("error", "Unknown Firecrawl error")
        raise RuntimeError(f"Firecrawl scrape failed: {error}")

    content = data.get("data", {}).get("markdown", "")
    if not content:
        content = data.get("data", {}).get("content", "")

    if not content:
        raise RuntimeError("Firecrawl returned no content")

    if max_length and len(content) > max_length:
        content = content[:max_length] + "\n\n... [content truncated]"

    return content


async def _fallback_scrape(url: str, max_length: int = 0) -> str:
    """Fallback scraper using httpx + trafilatura when Firecrawl is unavailable."""
    try:
        import trafilatura
    except ImportError:
        raise RuntimeError(
            "Neither FIRECRAWL_API_KEY nor trafilatura available for page scraping"
        ) from None

    client = await _get_client()
    resp = await client.get(url, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()

    content = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=True,
        include_links=True,
        favor_precision=True,
    )

    if not content:
        content = resp.text[:5000]
        return f"Could not extract structured content. Raw (truncated):\n\n{content}"

    if max_length and len(content) > max_length:
        content = content[:max_length] + "\n\n... [content truncated]"

    return content


async def _scrape_url(url: str, max_length: int = 0) -> str:
    """Scrape a URL — uses Firecrawl if available, falls back to trafilatura."""
    if FIRECRAWL_API_KEY:
        return await _firecrawl_scrape(url, max_length)
    return await _fallback_scrape(url, max_length)


# ---------------------------------------------------------------------------
# Search Tools
# ---------------------------------------------------------------------------


@mcp.tool("web_search")
async def web_search(
    query: str,
    max_results: int = MAX_SEARCH_RESULTS,
    country: str = "us",
    time_period: str | None = None,
) -> str:
    """Search the PUBLIC web using Google via Serper.

    Use this ONLY for questions about public information: current events,
    product research, technical documentation, how-to guides, or topics
    NOT related to Jack's personal life.

    Do NOT use this for: Jack's schedule, todos, preferences, health,
    finances, relationships, work plans, or anything he has discussed
    before. Use the Knowledge MCP context-pack tool for those — it searches
    Jack's personal Knowledge base.

    Run the Knowledge context-pack first. Only use web search when personal
    context is insufficient or the question is clearly about public data.

    Args:
        query: Search query string.
        max_results: Maximum results to return (default 10, max 50).
        country: Country code for localized results (default 'us').
        time_period: Time filter — 'd' (day), 'w' (week), 'm' (month),
            'y' (year), or None for all time.

    Returns:
        Formatted search results with titles, snippets, and URLs.
    """
    async with _search_semaphore:
        try:
            payload: dict[str, Any] = {
                "q": query,
                "num": min(max_results, 50),
                "gl": country,
            }
            if time_period:
                tbs_map = {"d": "qdr:d", "w": "qdr:w", "m": "qdr:m", "y": "qdr:y"}
                tbs = tbs_map.get(time_period)
                if tbs:
                    payload["tbs"] = tbs

            data = await _serper_request("search", payload)

            # Include answer box if present
            parts = []
            answer_box = data.get("answerBox")
            if answer_box:
                ab_title = answer_box.get("title", "")
                ab_answer = answer_box.get("answer") or answer_box.get("snippet", "")
                if ab_answer:
                    parts.append(f"**Answer:** {ab_answer}")
                    if ab_title:
                        parts.append(f"Source: {ab_title}")
                    parts.append("")

            # Include knowledge graph if present
            kg = data.get("knowledgeGraph")
            if kg:
                kg_title = kg.get("title", "")
                kg_type = kg.get("type", "")
                kg_desc = kg.get("description", "")
                if kg_title:
                    label = f"**{kg_title}**"
                    if kg_type:
                        label += f" ({kg_type})"
                    parts.append(label)
                    if kg_desc:
                        parts.append(kg_desc)
                    parts.append("")

            # Organic results
            organic = data.get("organic", [])
            if not organic and not parts:
                return f"No results found for '{query}'"

            # Rerank organic results
            organic = await _rerank_results(query, organic, text_key="snippet")

            for i, result in enumerate(organic, 1):
                parts.append(_format_organic_result(result, i))
                parts.append("")

            return f"Search results for: {query}\n\n" + "\n".join(parts)

        except Exception as exc:
            return f"Search failed: {exc}"


@mcp.tool("web_search_news")
async def web_search_news(
    query: str,
    max_results: int = MAX_SEARCH_RESULTS,
    country: str = "us",
) -> str:
    """Search for recent news articles using Google News via Serper.

    Use for public news and current events. Do NOT use for Jack's personal
    schedule, plans, or life questions — use the Knowledge MCP context-pack
    tool instead.

    Args:
        query: News search query string.
        max_results: Maximum results to return (default 10, max 50).
        country: Country code for localized results.

    Returns:
        Formatted news results with titles, sources, dates, and URLs.
    """
    async with _search_semaphore:
        try:
            payload: dict[str, Any] = {
                "q": query,
                "num": min(max_results, 50),
                "gl": country,
            }
            data = await _serper_request("news", payload)

            news = data.get("news", [])
            if not news:
                return f"No news results found for '{query}'"

            # Rerank news results
            news = await _rerank_results(query, news, text_key="snippet")

            formatted = []
            for i, result in enumerate(news, 1):
                formatted.append(_format_news_result(result, i))

            return f"News results for: {query}\n\n" + "\n\n".join(formatted)

        except Exception as exc:
            return f"News search failed: {exc}"


@mcp.tool("web_search_images")
async def web_search_images(
    query: str,
    max_results: int = 10,
    country: str = "us",
) -> str:
    """Search for images using Google Images via Serper.

    Returns image results with titles, URLs, and sources.

    Args:
        query: Image search query string.
        max_results: Maximum results to return (default 10, max 100).
        country: Country code for localized results.

    Returns:
        Formatted image results or error message.
    """
    async with _search_semaphore:
        try:
            payload: dict[str, Any] = {
                "q": query,
                "num": min(max_results, 100),
                "gl": country,
            }
            data = await _serper_request("images", payload)

            images = data.get("images", [])
            if not images:
                return f"No image results found for '{query}'"

            formatted = []
            for i, result in enumerate(images, 1):
                title = result.get("title", "No title")
                url = result.get("imageUrl", "")
                thumbnail = result.get("thumbnailUrl", "")
                source = result.get("source", "")
                link = result.get("link", "")

                parts = [f"--- Image {i} ---", f"**{title}**"]
                if source:
                    parts.append(f"Source: {source}")
                if url:
                    parts.append(f"Image URL: {url}")
                if link:
                    parts.append(f"Page URL: {link}")
                if thumbnail:
                    parts.append(f"Thumbnail: {thumbnail}")

                formatted.append("\n".join(parts))

            return f"Image results for: {query}\n\n" + "\n\n".join(formatted)

        except Exception as exc:
            return f"Image search failed: {exc}"


# ---------------------------------------------------------------------------
# Content Fetching Tools (Firecrawl with trafilatura fallback)
# ---------------------------------------------------------------------------


@mcp.tool("web_fetch_page")
async def web_fetch_page(url: str) -> str:
    """Fetch and extract the main content from a public webpage.

    Uses Firecrawl for JavaScript-rendered scraping with clean markdown
    output. Falls back to basic HTTP + trafilatura if Firecrawl is
    unavailable.

    Use only after web_search has identified a relevant URL. Do not use this
    as a substitute for searching Jack's Knowledge base.

    Args:
        url: URL of the webpage to fetch.

    Returns:
        Extracted text content in markdown format, or error message.
    """
    async with _search_semaphore:
        try:
            return await _scrape_url(url)
        except Exception as exc:
            return f"Failed to fetch page: {exc}"


@mcp.tool("web_summarize_page")
async def web_summarize_page(url: str, max_length: int = 2000) -> str:
    """Fetch a webpage and return a truncated summary of its content.

    Uses Firecrawl for high-quality extraction, truncated to the specified
    character limit.

    Args:
        url: URL of the webpage to fetch and summarize.
        max_length: Maximum length of the summary in characters (default 2000).

    Returns:
        Summarized content or error message.
    """
    async with _search_semaphore:
        try:
            content = await _scrape_url(url, max_length=max_length)
            return f"Summary of {url}:\n\n{content}"
        except Exception as exc:
            return f"Failed to summarize page: {exc}"


# ---------------------------------------------------------------------------
# Entry Points
# ---------------------------------------------------------------------------


def run() -> None:
    """Run the web_search MCP server."""
    mcp.run(transport="streamable-http", host="0.0.0.0", port=DEFAULT_HTTP_PORT)


def main() -> None:
    """CLI entry point for the web_search MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Web Search MCP Server")
    parser.add_argument(
        "--transport", default="streamable-http", choices=["streamable-http", "stdio"]
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)

    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
