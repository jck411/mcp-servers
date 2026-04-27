"""Standalone web search MCP server with free, no-API-key search backends.

Provides web search via DuckDuckGo and webpage content extraction using
trafilatura. Zero external API keys required — fully standalone.

Features:
- DuckDuckGo web search (free, no API key)
- Webpage content extraction (trafilatura)
- News search
- Image search
- Automatic backend selection with fallback

Run:
    python -m servers.web_search --transport streamable-http --host 0.0.0.0 --port 9016
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import trafilatura
from duckduckgo_search import DDGS
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 9016
SEARCH_TIMEOUT = 15  # seconds per search
FETCH_TIMEOUT = 20  # seconds per page fetch
MAX_SEARCH_RESULTS = 10  # default max results

# Limit concurrent requests to avoid rate limiting
_search_semaphore = asyncio.Semaphore(int(os.getenv("WEB_SEARCH_CONCURRENCY", "5")))

mcp = FastMCP("web_search")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_search_result(result: dict[str, str]) -> str:
    """Format a single search result into readable text."""
    title = result.get("title", "No title")
    body = result.get("body", "")
    href = result.get("href", "")
    
    parts = [f"**{title}**"]
    if body:
        parts.append(body)
    if href:
        parts.append(f"URL: {href}")
    
    return "\n".join(parts)


async def _call_with_timeout(func: Any, *args: Any, timeout: int = SEARCH_TIMEOUT, **kwargs: Any) -> Any:
    """Call a synchronous function with a timeout to avoid hanging."""
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, lambda: func(*args, **kwargs)),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Search Tools
# ---------------------------------------------------------------------------


@mcp.tool("web_search")
async def web_search(
    query: str,
    max_results: int = MAX_SEARCH_RESULTS,
    region: str = "wt-wt",
    timelimit: str | None = None,
) -> str:
    """Search the web using DuckDuckGo.

    Performs a general web search and returns formatted results with titles,
    snippets, and URLs.

    Args:
        query: Search query string
        max_results: Maximum number of results to return (default: 10, max: 50)
        region: Region code for localized results (e.g., 'us-en', 'uk-en', 'wt-wt' for worldwide)
        timelimit: Time filter — 'd' (day), 'w' (week), 'm' (month), 'y' (year), or None

    Returns:
        Formatted search results or error message.
    """
    async with _search_semaphore:
        try:
            results = await _call_with_timeout(
                DDGS().text,
                keywords=query,
                region=region,
                safesearch="moderate",
                timelimit=timelimit,
                max_results=min(max_results, 50),
                timeout=SEARCH_TIMEOUT,
            )
            
            if not results:
                return f"No results found for '{query}'"
            
            formatted = []
            for i, result in enumerate(results, 1):
                formatted.append(f"--- Result {i} ---\n{_format_search_result(result)}")
            
            return f"Search results for: {query}\n\n" + "\n\n".join(formatted)
            
        except Exception as exc:
            return f"Search failed: {exc}"


@mcp.tool("web_search_news")
async def web_search_news(
    query: str,
    max_results: int = MAX_SEARCH_RESULTS,
    region: str = "wt-wt",
) -> str:
    """Search for recent news articles using DuckDuckGo News.

    Returns news articles with titles, sources, dates, and URLs.

    Args:
        query: News search query string
        max_results: Maximum number of results to return (default: 10, max: 50)
        region: Region code for localized results

    Returns:
        Formatted news results or error message.
    """
    async with _search_semaphore:
        try:
            results = await _call_with_timeout(
                DDGS().news,
                keywords=query,
                region=region,
                safesearch="moderate",
                max_results=min(max_results, 50),
                timeout=SEARCH_TIMEOUT,
            )
            
            if not results:
                return f"No news results found for '{query}'"
            
            formatted = []
            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                body = result.get("body", "")
                url = result.get("url", "")
                source = result.get("source", "")
                date = result.get("date", "")
                
                parts = [f"--- News {i} ---", f"**{title}**"]
                if source:
                    parts.append(f"Source: {source}")
                if date:
                    parts.append(f"Date: {date}")
                if body:
                    parts.append(body)
                if url:
                    parts.append(f"URL: {url}")
                
                formatted.append("\n".join(parts))
            
            return f"News results for: {query}\n\n" + "\n\n".join(formatted)
            
        except Exception as exc:
            return f"News search failed: {exc}"


@mcp.tool("web_search_images")
async def web_search_images(
    query: str,
    max_results: int = 10,
    size: str | None = None,
    color: str | None = None,
) -> str:
    """Search for images using DuckDuckGo Images.

    Returns image results with titles, URLs, and thumbnail links.

    Args:
        query: Image search query string
        max_results: Maximum number of results to return (default: 10, max: 100)
        size: Image size filter — 'Small', 'Medium', 'Large', 'Wallpaper', or None
        color: Color filter — 'color', 'Monochrome', 'Red', 'Orange', 'Yellow',
               'Green', 'Blue', 'Purple', 'Pink', 'White', 'Gray', 'Black', or None

    Returns:
        Formatted image results or error message.
    """
    async with _search_semaphore:
        try:
            results = await _call_with_timeout(
                DDGS().images,
                keywords=query,
                safesearch="moderate",
                size=size,
                color=color,
                max_results=min(max_results, 100),
                timeout=SEARCH_TIMEOUT,
            )
            
            if not results:
                return f"No image results found for '{query}'"
            
            formatted = []
            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                url = result.get("image", "")
                thumbnail = result.get("thumbnail", "")
                source = result.get("source", "")
                
                parts = [f"--- Image {i} ---", f"**{title}**"]
                if source:
                    parts.append(f"Source: {source}")
                if url:
                    parts.append(f"Image URL: {url}")
                if thumbnail:
                    parts.append(f"Thumbnail: {thumbnail}")
                
                formatted.append("\n".join(parts))
            
            return f"Image results for: {query}\n\n" + "\n\n".join(formatted)
            
        except Exception as exc:
            return f"Image search failed: {exc}"


# ---------------------------------------------------------------------------
# Content Fetching Tools
# ---------------------------------------------------------------------------


@mcp.tool("web_fetch_page")
async def web_fetch_page(url: str) -> str:
    """Fetch and extract the main content from a webpage.

    Downloads a webpage and extracts the main text content, removing navigation,
    ads, and other non-essential elements. Returns clean, readable text.

    Args:
        url: URL of the webpage to fetch

    Returns:
        Extracted text content or error message.
    """
    async with _search_semaphore:
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
            
            # Use trafilatura to extract main content
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                include_links=True,
                favor_precision=True,
            )
            
            if not content:
                # Fallback: return plain text if extraction fails
                content = response.text[:5000]  # Limit to first 5000 chars
                return f"Could not extract structured content. Raw content (truncated):\n\n{content}"
            
            return content
            
        except httpx.HTTPStatusError as exc:
            return f"HTTP error fetching page: {exc.response.status_code} {exc.response.reason_phrase}"
        except httpx.RequestError as exc:
            return f"Request failed: {exc}"
        except Exception as exc:
            return f"Failed to fetch page: {exc}"


@mcp.tool("web_summarize_page")
async def web_summarize_page(url: str, max_length: int = 2000) -> str:
    """Fetch a webpage and return a truncated summary of its content.

    Downloads a webpage, extracts the main content, and returns a summary
    limited to the specified character count.

    Args:
        url: URL of the webpage to fetch and summarize
        max_length: Maximum length of the summary in characters (default: 2000)

    Returns:
        Summarized content or error message.
    """
    async with _search_semaphore:
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
            
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
            
            if not content:
                return f"Could not extract content from {url}"
            
            # Truncate to max_length
            if len(content) > max_length:
                content = content[:max_length] + "\n\n... [content truncated]"
            
            return f"Summary of {url}:\n\n{content}"
            
        except httpx.HTTPStatusError as exc:
            return f"HTTP error fetching page: {exc.response.status_code} {exc.response.reason_phrase}"
        except httpx.RequestError as exc:
            return f"Request failed: {exc}"
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
    parser.add_argument("--transport", default="streamable-http", choices=["streamable-http", "stdio"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    
    args = parser.parse_args()
    
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
