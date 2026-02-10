"""Standalone PDF / document-intelligence MCP server.

Wraps the Kreuzberg library to expose document extraction tools via MCP.
Adds URL-aware extraction (HTTP/HTTPS downloads, Google Drive link
normalization).  Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.pdf --transport streamable-http --host 0.0.0.0 --port 9007
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP
import httpx
import kreuzberg
from kreuzberg import (
    ChunkingConfig,
    ExtractionConfig,
    KeywordConfig,
    LanguageDetectionConfig,
    OcrConfig,
)

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9007

mcp = FastMCP("pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_http_url(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


def _normalize_gdrive_url(url: str) -> str:
    """Transform Google Drive URLs to direct download format."""
    if "drive.google.com" not in url:
        return url

    file_id: str | None = None

    if "/file/d/" in url:
        parts = url.split("/file/d/")[1].split("/")[0]
        file_id = parts.split("?")[0]
    elif "open?id=" in url:
        file_id = url.split("open?id=")[1].split("&")[0]
    elif "uc?id=" in url:
        file_id = url.split("uc?id=")[1].split("&")[0]

    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def _guess_mime_from_suffix(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".pdf":
        return "application/pdf"
    if suf in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return f"image/{suf.lstrip('.').replace('jpg', 'jpeg')}"
    if suf in {".txt", ".md"}:
        return "text/plain"
    return "application/octet-stream"


def _convert_extraction_result(result: Any) -> dict[str, Any]:
    """Convert ExtractionResult to a JSON-serializable dictionary."""
    if isinstance(result, dict):
        return result

    data: dict[str, Any] = {
        "content": getattr(result, "content", ""),
        "mime_type": getattr(result, "mime_type", None),
    }

    chunks = getattr(result, "chunks", None)
    if chunks:
        data["chunks"] = [str(c) for c in chunks]

    detected_languages = getattr(result, "detected_languages", None)
    if detected_languages:
        data["detected_languages"] = list(detected_languages)

    metadata = getattr(result, "metadata", None)
    if metadata:
        data["metadata"] = str(metadata)

    tables = getattr(result, "tables", None)
    if tables:
        data["tables"] = [
            {
                "page_number": getattr(t, "page_number", None),
                "markdown": getattr(t, "markdown", None),
            }
            for t in tables
        ]

    images = getattr(result, "images", None)
    if images:
        data["images"] = [
            {
                "format": getattr(i, "format", None),
                "filename": getattr(i, "filename", None),
                "page_number": getattr(i, "page_number", None),
            }
            for i in images
        ]

    return data


def _build_extraction_config(
    *,
    force_ocr: bool = False,
    ocr_backend: str = "tesseract",
    ocr_language: Optional[str] = None,
    chunk_content: bool = False,
    max_chars: int = 1000,
    max_overlap: int = 200,
    extract_keywords: bool = False,
    keyword_count: int = 10,
    auto_detect_language: bool = False,
) -> ExtractionConfig:
    """Build an ExtractionConfig from flat tool arguments (kreuzberg 4.x)."""
    ocr: OcrConfig | None = None
    if force_ocr or ocr_backend != "tesseract" or ocr_language:
        ocr = OcrConfig(
            backend=ocr_backend if ocr_backend != "tesseract" else None,
            language=ocr_language,
        )

    chunking: ChunkingConfig | None = None
    if chunk_content:
        chunking = ChunkingConfig(max_chars=max_chars, max_overlap=max_overlap)

    keywords: KeywordConfig | None = None
    if extract_keywords:
        keywords = KeywordConfig(max_keywords=keyword_count)

    lang_detection: LanguageDetectionConfig | None = None
    if auto_detect_language:
        lang_detection = LanguageDetectionConfig(enabled=True)

    return ExtractionConfig(
        force_ocr=force_ocr or None,
        ocr=ocr,
        chunking=chunking,
        keywords=keywords,
        language_detection=lang_detection,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool("pdf_extract_document")
async def extract_document(  # noqa: PLR0913
    document_url_or_path: str,
    mime_type: Optional[str] = None,
    force_ocr: bool = False,
    chunk_content: bool = False,
    extract_keywords: bool = False,
    ocr_backend: str = "tesseract",
    ocr_language: Optional[str] = None,
    max_chars: int = 1000,
    max_overlap: int = 200,
    keyword_count: int = 10,
    auto_detect_language: bool = False,
) -> dict[str, Any]:
    """Extract document content from filesystem paths or URLs.

    Handles local file paths and web URLs (including Google Drive share links).
    URLs are downloaded automatically; local files are read directly.

    Args:
        document_url_or_path: Local filesystem path OR web URL (http/https)
        mime_type: Optional MIME type hint
        force_ocr: Force OCR even for text-based documents
        chunk_content: Split content into chunks for RAG
        extract_keywords: Extract keywords with scores
        ocr_backend: OCR backend ('tesseract', 'easyocr', 'paddleocr')
        ocr_language: Language hint for OCR
        max_chars: Maximum characters per chunk
        max_overlap: Character overlap between chunks
        keyword_count: Number of keywords to extract
        auto_detect_language: Auto-detect document language
    """
    config = _build_extraction_config(
        force_ocr=force_ocr,
        ocr_backend=ocr_backend,
        ocr_language=ocr_language,
        chunk_content=chunk_content,
        max_chars=max_chars,
        max_overlap=max_overlap,
        extract_keywords=extract_keywords,
        keyword_count=keyword_count,
        auto_detect_language=auto_detect_language,
    )

    # Strip file:// scheme
    if document_url_or_path.startswith("file://"):
        document_url_or_path = document_url_or_path[7:]

    # --- Local file path ---
    if not _is_http_url(document_url_or_path):
        try:
            p = Path(document_url_or_path)
            if p.is_absolute() and p.is_file():
                content_bytes = await asyncio.to_thread(p.read_bytes)
                effective_mime = (
                    mime_type or _guess_mime_from_suffix(p) or "application/octet-stream"
                ).lower()
                result = await kreuzberg.extract_bytes(content_bytes, effective_mime, config)
                return _convert_extraction_result(result)
        except Exception:
            pass

        # Fallback: delegate to kreuzberg extract_file
        try:
            result = await kreuzberg.extract_file(document_url_or_path, mime_type, config)
            return _convert_extraction_result(result)
        except Exception as e:
            return {"error": str(e)}

    # --- HTTP(S) URL ---
    if "drive.google.com" in document_url_or_path:
        document_url_or_path = _normalize_gdrive_url(document_url_or_path)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            resp = await client.get(document_url_or_path)
        resp.raise_for_status()
    except Exception as exc:
        return {"error": f"Failed to download URL: {exc}", "url": document_url_or_path}

    content = resp.content
    if not content:
        return {"error": "Downloaded file was empty", "url": document_url_or_path}

    inferred_mime: str | None = None
    try:
        ct = resp.headers.get("Content-Type") or ""
        inferred_mime = ct.split(";")[0].strip() or None
    except Exception:
        pass

    effective_mime = (mime_type or inferred_mime or "application/octet-stream").lower()
    result = await kreuzberg.extract_bytes(content, effective_mime, config)
    return _convert_extraction_result(result)


@mcp.tool("pdf_extract_bytes")
async def extract_bytes_tool(  # noqa: PLR0913
    content_base64: str,
    mime_type: str,
    force_ocr: bool = False,
    chunk_content: bool = False,
    extract_keywords: bool = False,
    ocr_backend: str = "tesseract",
    ocr_language: Optional[str] = None,
    max_chars: int = 1000,
    max_overlap: int = 200,
    keyword_count: int = 10,
    auto_detect_language: bool = False,
) -> dict[str, Any]:
    """Extract text from base64-encoded document data already in memory.

    Use pdf_extract_document instead for files on disk or URLs.

    Args:
        content_base64: Base64-encoded document content
        mime_type: MIME type (e.g. 'application/pdf', 'image/jpeg')
        force_ocr: Force OCR even for text-based documents
        chunk_content: Split content into chunks
        extract_keywords: Extract keywords with scores
        ocr_backend: OCR backend ('tesseract', 'easyocr', 'paddleocr')
        ocr_language: Language hint for OCR
        max_chars: Maximum characters per chunk
        max_overlap: Character overlap between chunks
        keyword_count: Number of keywords to extract
        auto_detect_language: Auto-detect document language
    """
    config = _build_extraction_config(
        force_ocr=force_ocr,
        ocr_backend=ocr_backend,
        ocr_language=ocr_language,
        chunk_content=chunk_content,
        max_chars=max_chars,
        max_overlap=max_overlap,
        extract_keywords=extract_keywords,
        keyword_count=keyword_count,
        auto_detect_language=auto_detect_language,
    )
    content_bytes = base64.b64decode(content_base64)
    result = await kreuzberg.extract_bytes(content_bytes, mime_type, config)
    return _convert_extraction_result(result)


@mcp.tool("pdf_batch_extract_bytes")
async def batch_extract_bytes_tool(
    contents_base64: list[str],
    mime_types: list[str],
    force_ocr: bool = False,
    chunk_content: bool = False,
    extract_keywords: bool = False,
    ocr_backend: str = "tesseract",
    max_chars: int = 1000,
    max_overlap: int = 200,
    keyword_count: int = 10,
    auto_detect_language: bool = False,
) -> list[dict[str, Any]]:
    """Process multiple in-memory base64-encoded documents concurrently.

    Args:
        contents_base64: List of base64-encoded document contents
        mime_types: List of MIME types (must match length of contents_base64)
        force_ocr: Force OCR even for text-based documents
        chunk_content: Split content into chunks
        extract_keywords: Extract keywords with scores
        ocr_backend: OCR backend to use
        max_chars: Maximum characters per chunk
        max_overlap: Character overlap between chunks
        keyword_count: Number of keywords to extract
        auto_detect_language: Auto-detect document languages
    """
    config = _build_extraction_config(
        force_ocr=force_ocr,
        ocr_backend=ocr_backend,
        chunk_content=chunk_content,
        max_chars=max_chars,
        max_overlap=max_overlap,
        extract_keywords=extract_keywords,
        keyword_count=keyword_count,
        auto_detect_language=auto_detect_language,
    )
    data_list = [base64.b64decode(b64) for b64 in contents_base64]
    results = await kreuzberg.batch_extract_bytes(data_list, mime_types, config)
    return [_convert_extraction_result(r) for r in results]


@mcp.tool("pdf_batch_extract_files")
async def batch_extract_files_tool(
    file_paths: list[str],
    force_ocr: bool = False,
    chunk_content: bool = False,
    extract_keywords: bool = False,
    ocr_backend: str = "tesseract",
    max_chars: int = 1000,
    max_overlap: int = 200,
    keyword_count: int = 10,
    auto_detect_language: bool = False,
) -> list[dict[str, Any]]:
    """Process multiple documents from file paths concurrently.

    Args:
        file_paths: List of document file paths
        force_ocr: Force OCR even for text-based documents
        chunk_content: Split content into chunks
        extract_keywords: Extract keywords with scores
        ocr_backend: OCR backend to use
        max_chars: Maximum characters per chunk
        max_overlap: Character overlap between chunks
        keyword_count: Number of keywords to extract
        auto_detect_language: Auto-detect document languages
    """
    config = _build_extraction_config(
        force_ocr=force_ocr,
        ocr_backend=ocr_backend,
        chunk_content=chunk_content,
        max_chars=max_chars,
        max_overlap=max_overlap,
        extract_keywords=extract_keywords,
        keyword_count=keyword_count,
        auto_detect_language=auto_detect_language,
    )
    results = await kreuzberg.batch_extract_files(file_paths, config)
    return [_convert_extraction_result(r) for r in results]


@mcp.tool("pdf_extract_simple")
async def extract_simple_tool(
    file_path: str,
    mime_type: Optional[str] = None,
) -> str:
    """Quick text extraction without advanced features.

    Lightweight — only extracts plain text.  Use pdf_extract_document if you need
    OCR control, keyword extraction, or chunking.

    Args:
        file_path: Path to the document file
        mime_type: Optional MIME type hint
    """
    result = await kreuzberg.extract_file(file_path, mime_type)
    return result.content


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the PDF MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:  # pragma: no cover - CLI helper
    import argparse

    parser = argparse.ArgumentParser(description="PDF MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "run",
    "main",
    "DEFAULT_HTTP_PORT",
    "extract_document",
    "extract_bytes_tool",
    "batch_extract_bytes_tool",
    "batch_extract_files_tool",
    "extract_simple_tool",
]
