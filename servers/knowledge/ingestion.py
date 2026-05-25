"""File ingestion pipeline and fact extraction for the Knowledge service.

Extracted from knowledge_server.py during Phase 3 modularization.
Contains _ingest_file_at_path and extract_source_facts_single_shot.
"""

from __future__ import annotations

import base64
import contextlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from servers.knowledge.db import KnowledgeDB
from servers.knowledge.embeddings import BM25SparseEncoder, EmbeddingClient
from servers.knowledge.extraction import (
    _IMAGE_MEDIA,
    EXTRACTION_SYSTEM_PROMPT,
    IMAGE_EXTENSIONS,
    _decode_llm_json_object,
    _extract_and_chunk_with_log,
    compute_file_hash,
)
from servers.knowledge.settings import KnowledgeSettings
from servers.knowledge.vectors import KnowledgeVectorStore
from servers.knowledge_source_files import (
    resolve_source_path,
    source_media_type,
    source_relative_path,
)
from shared.logging_config import get_logger

log = get_logger("knowledge")


# ---------------------------------------------------------------------------
# Shared ingestion pipeline
# ---------------------------------------------------------------------------

# File extensions that imply binary/document uploads. These must never be
# accepted as a `source_name` for `knowledge_ingest_text` — that path stores
# only chunks (no `stored_path`, no raw bytes), so a `.pdf` source created via
# text ingest is silently a fake file. Real binary uploads must go through the
# upload API or admin file placement followed by `knowledge_ingest_file`.
_BINARY_NAME_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff",
    ".webp", ".bmp", ".gif", ".svg",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg",
    ".mp4", ".mov", ".mkv", ".avi", ".webm",
    ".epub", ".mobi",
})

# `source_type` values that `knowledge_ingest_text` is allowed to record. This
# blocks an agent from labeling a text source as `identity_document`,
# `pdf`, etc., which previously hid text-only rows behind binary-looking types.
_TEXT_SOURCE_TYPE_ALLOWLIST: frozenset[str] = frozenset({
    "note", "summary", "transcript", "research", "caption",
    "markdown", "text", "manual", "chat", "memo",
})


def _validate_text_ingest_inputs(
    source_name: str,
    source_type: str,
) -> str | None:
    """Return an error message if text-ingest inputs look like a binary upload."""
    name_ext = Path(source_name).suffix.lower()
    if name_ext in _BINARY_NAME_EXTENSIONS:
        return (
            f"source_name '{source_name}' has a binary/document extension "
            f"({name_ext}). Use POST /api/upload/{{domain}} or place the file "
            "under the domain directory and call knowledge_ingest_file so the "
            "original bytes are stored. "
            "knowledge_ingest_text only stores extracted text chunks."
        )
    type_lower = source_type.lower().strip()
    if type_lower not in _TEXT_SOURCE_TYPE_ALLOWLIST:
        if type_lower.lstrip(".") in {ext.lstrip(".") for ext in _BINARY_NAME_EXTENSIONS}:
            return (
                f"source_type '{source_type}' looks like a file extension. "
                "Use POST /api/upload/{domain} or knowledge_ingest_file for the actual file."
            )
        allowed = ", ".join(sorted(_TEXT_SOURCE_TYPE_ALLOWLIST))
        return (
            f"source_type '{source_type}' is not allowed for text ingest. "
            f"Use one of: {allowed}."
        )
    return None


async def _ingest_file_at_path(
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    *,
    dest: Path,
    domain: str,
    force: bool = False,
) -> dict[str, Any]:
    """Hash, extract, embed, and persist one file already on disk under `dest`.

    Shared by `POST /api/upload/{domain}` and `knowledge_ingest_file`.
    Returns a result dict; never raises for
    "no content" / "already ingested" — those are normal outcomes.
    """
    file_hash = compute_file_hash(dest)
    rel_path = source_relative_path(settings.knowledge_path, dest)
    media_type = source_media_type(dest.name)
    size_bytes = dest.stat().st_size

    existing = await db.source_get_by_hash(file_hash, domain=domain)
    if existing and not force:
        existing_id = str(existing.get("id") or "")
        existing_path = existing.get("stored_path")
        if not existing_path:
            # Legacy text-only row — backfill the stored bytes onto the same source_id.
            await db.source_update_storage(
                existing_id,
                stored_path=rel_path,
                media_type=media_type,
                size_bytes=size_bytes,
            )
            return {
                "success": True,
                "file": dest.name,
                "domain": domain,
                "ingested": False,
                "source_id": existing_id,
                "stored_path": rel_path,
                "reason": "backfilled stored bytes onto existing source",
            }
        # Already have bytes for this hash — drop the freshly-written duplicate.
        try:
            if rel_path != existing_path and dest.exists():
                dest.unlink()
        except OSError:
            pass
        return {
            "success": True,
            "file": dest.name,
            "domain": domain,
            "ingested": False,
            "source_id": existing_id,
            "stored_path": existing_path,
            "reason": "already ingested with stored bytes",
        }

    chunks_text, pipeline_log, pipeline_type = await _extract_and_chunk_with_log(dest, settings)

    if not chunks_text:
        # No text extracted (e.g. photo with description model skipped/failed, or
        # unsupported binary). Register the source so bytes are downloadable.
        source_id = str(uuid.uuid4())
        source_type = dest.suffix.lstrip(".") or "file"
        await db.source_add(
            source_id, domain, source_type, dest.name,
            file_hash, 0, rel_path, media_type, size_bytes,
        )
        # Determine a helpful reason from the pipeline log
        failed = [s for s in pipeline_log if s.get("status") == "failed"]
        if failed:
            reason = f"pipeline step '{failed[0]['step']}' failed: {failed[0].get('note', '')}"
        elif pipeline_type == "image_description":
            reason = "image stored — use Extract Facts to generate a searchable description"
        elif pipeline_type == "unsupported":
            reason = "unsupported file type — bytes stored only"
        else:
            reason = "no extractable text — bytes stored"
        return {
            "success": True,
            "file": dest.name,
            "domain": domain,
            "ingested": True,
            "source_id": source_id,
            "chunks_stored": 0,
            "stored_path": rel_path,
            "pipeline_type": pipeline_type,
            "pipeline": pipeline_log,
            "needs_extraction": pipeline_type in ("image_description", "document_ocr"),
            "reason": reason,
        }

    sparse_encoder.fit_batch(chunks_text)
    sparse_vecs = [sparse_encoder.encode(t) for t in chunks_text]
    dense_vecs = await embeddings.embed_batch(chunks_text)

    source_id = str(uuid.uuid4())
    source_type = dest.suffix.lstrip(".") or "file"
    now = datetime.now(UTC).isoformat()
    chunk_payloads = [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_{i}")),
            "domain": domain,
            "source_id": source_id,
            "source_type": source_type,
            "source_name": dest.name,
            "chunk_index": i,
            "content": text,
            "ingested_at": now,
        }
        for i, text in enumerate(chunks_text)
    ]

    await vectors.upsert_chunks(chunk_payloads, dense_vecs, sparse_vecs)
    await db.source_add(
        source_id, domain, source_type, dest.name,
        file_hash, len(chunks_text), rel_path, media_type, size_bytes,
    )

    warn_steps = [s for s in pipeline_log if s.get("status") == "warn"]
    return {
        "success": True,
        "file": dest.name,
        "domain": domain,
        "ingested": True,
        "source_id": source_id,
        "chunks_stored": len(chunks_text),
        "stored_path": rel_path,
        "pipeline_type": pipeline_type,
        "pipeline": pipeline_log,
        "needs_extraction": bool(warn_steps),
    }


# ---------------------------------------------------------------------------
# Single-shot fact extraction (POST /api/sources/{id}/extract)
# ---------------------------------------------------------------------------


async def extract_source_facts_single_shot(
    settings: KnowledgeSettings,
    embeddings: EmbeddingClient,
    sparse_encoder: BM25SparseEncoder,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    source_id: str,
    hint: str | None = None,
) -> dict[str, Any]:
    """Single-shot Sonnet extraction: one LLM call → structured facts + optional caption.

    For images and sources with no chunks: loads raw bytes directly.
    For text documents: uses existing Qdrant chunks (much cheaper — no image token cost).
    Uses Anthropic prompt caching on the system prompt when using Claude models.
    """
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    if not settings.extraction_model:
        return {"success": False, "error": "KNOWLEDGE_EXTRACTION_MODEL not configured"}

    pipeline: list[dict[str, Any]] = []
    suffix = Path(str(source.get("filename") or "")).suffix.lower()
    is_image = suffix in IMAGE_EXTENSIONS
    chunk_count = int(source.get("chunk_count") or 0)
    domain = str(source.get("domain") or "")

    # --- Step 1: gather content ---
    user_content: str | list[dict[str, Any]]

    if is_image:
        source_path = resolve_source_path(settings.knowledge_path, source)
        if not source_path:
            pipeline.append({
                "step": "load_source", "status": "failed",
                "note": "file not found on disk",
            })
            return {
                "success": False,
                "error": "Source file not found on disk",
                "pipeline": pipeline,
            }
        try:
            image_bytes = source_path.read_bytes()
            image_media_type = _IMAGE_MEDIA.get(suffix, "image/png")
            pipeline.append({
                "step": "load_source", "status": "ok",
                "note": f"{len(image_bytes)} bytes read from disk",
            })
        except OSError as exc:
            pipeline.append({
                "step": "load_source", "status": "failed", "note": str(exc),
            })
            return {
                "success": False,
                "error": f"Could not read source file: {exc}",
                "pipeline": pipeline,
            }

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_media_type};base64,{b64}"
        hint_text = f"\nDocument type hint: {hint}" if hint else ""
        user_content = [
            {"type": "text", "text": f"Extract all information from this document.{hint_text}"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    elif chunk_count == 0:
        pipeline.append({
            "step": "load_chunks", "status": "failed",
            "note": "source has no indexed text chunks and is not a supported image",
        })
        return {
            "success": False,
            "error": (
                "Source has no indexed text chunks; "
                "Extract Facts only supports images or indexed text"
            ),
            "pipeline": pipeline,
        }
    else:
        chunks = await vectors.chunks_by_source(source_id)
        text_body = "\n\n".join(
            str(c.get("content") or "").strip() for c in chunks if c.get("content")
        )
        pipeline.append({
            "step": "load_chunks", "status": "ok",
            "note": f"{len(chunks)} chunks, {len(text_body)} chars total",
        })
        if not text_body.strip():
            return {
                "success": False,
                "error": "No stored chunk text found for source",
                "pipeline": pipeline,
            }
        hint_text = f"\nDocument type hint: {hint}" if hint else ""
        user_content = (
            f"Extract all information from this document.{hint_text}\n\n---\n\n{text_body}"
        )

    # --- Step 2: call extraction model ---
    is_claude = "anthropic" in settings.extraction_model or "claude" in settings.extraction_model
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": (
            user_content if isinstance(user_content, list)
            else [{"type": "text", "text": user_content}]
        ),
    }]

    # Forced tool use: the model MUST call store_extracted_facts, so it returns
    # structured JSON regardless of whether it would otherwise output markdown.
    # This is the only reliable way to get structured output from Claude via OpenRouter.
    _extract_tool: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "store_extracted_facts",
            "description": "Store all facts extracted from the document",
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "object",
                        "description": (
                            "All extracted key-value pairs using stable snake_case keys "
                            "with meaningful prefixes (e.g. w2_2025_box1_wages). "
                            "Dates in ISO format YYYY-MM-DD. Currency as numbers only."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "caption": {
                        "type": ["string", "null"],
                        "description": (
                            "2-3 sentence description for photos/images with no document "
                            "structure. null for documents."
                        ),
                    },
                    "warnings": {
                        "type": "array",
                        "description": "Brief notes about unreadable, obscured, or skipped values.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["facts", "caption"],
            },
        },
    }

    payload: dict[str, Any] = {
        "model": settings.extraction_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 4096,
        "tools": [_extract_tool],
        "tool_choice": {"type": "function", "function": {"name": "store_extracted_facts"}},
    }

    # Anthropic prompt caching: wrap system prompt in a content block with
    # cache_control so repeated calls within 5 min read from cache at 90% discount.
    if is_claude:
        payload["system"] = [{
            "type": "text",
            "text": EXTRACTION_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        payload["system"] = EXTRACTION_SYSTEM_PROMPT

    llm_step: dict[str, Any] = {
        "step": "extraction_llm",
        "model": settings.extraction_model,
        "status": "failed",
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read_tokens": 0,
        "note": "",
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    raw_output = ""
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                # Forced tool call — arguments are already structured JSON
                raw_output = tool_calls[0]["function"]["arguments"]
            else:
                # Fallback: model returned text content instead of a tool call
                raw_output = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            llm_step["tokens_in"] = usage.get("prompt_tokens", 0)
            llm_step["tokens_out"] = usage.get("completion_tokens", 0)
            llm_step["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
            llm_step["status"] = "ok"
            output_type = "tool_call" if tool_calls else "text"
            cache_note = (
                f", {llm_step['cache_read_tokens']} cached tokens"
                if llm_step["cache_read_tokens"]
                else ""
            )
            llm_step["note"] = f"{output_type}, {len(raw_output)} chars{cache_note}"
    except Exception as exc:  # noqa: BLE001
        # Capture response body for HTTP errors to expose the provider's error message
        body = ""
        if hasattr(exc, "response") and exc.response is not None:  # type: ignore[union-attr]
            with contextlib.suppress(Exception):
                body = exc.response.text[:500]  # type: ignore[union-attr]
        llm_step["note"] = f"{exc}" + (f" | body: {body}" if body else "")
        pipeline.append(llm_step)
        return {"success": False, "error": f"LLM call failed: {exc}", "pipeline": pipeline}
    pipeline.append(llm_step)

    # --- Step 3: parse JSON ---
    parse_step: dict[str, Any] = {"step": "parse_json", "model": None}
    try:
        extracted = _decode_llm_json_object(raw_output)
        raw_caption = extracted.get("caption")
        caption: str | None = str(raw_caption) if raw_caption not in (None, "") else None
        raw_warnings = extracted.get("warnings") or []
        if isinstance(raw_warnings, list):
            warnings = [str(w) for w in raw_warnings if w]
        elif raw_warnings:
            warnings = [str(raw_warnings)]
        else:
            warnings = []
        if "facts" in extracted:
            raw_facts = extracted.get("facts") or {}
            if not isinstance(raw_facts, dict):
                raise ValueError("'facts' must be an object")
            facts: dict[str, str] = {
                str(k): str(v) for k, v in raw_facts.items()
                if v is not None and v != ""
            }
        else:
            facts = {
                str(k): str(v) for k, v in extracted.items()
                if k not in {"caption", "warnings"} and v is not None and v != ""
            }
        if is_image and caption and facts and not hint:
            warnings.append(
                f"Skipped {len(facts)} photo-detail fact(s); ordinary photos use captions."
            )
            facts = {}
        parse_step["status"] = "ok"
        warn_note = f", {len(warnings)} warning(s)" if warnings else ""
        parse_step["note"] = (
            f"{len(facts)} fact(s), caption={'yes' if caption else 'no'}{warn_note}"
        )
    except (json.JSONDecodeError, ValueError) as exc:
        parse_step["status"] = "failed"
        parse_step["note"] = f"JSON parse error: {exc} | raw[:200]: {raw_output[:200]}"
        pipeline.append(parse_step)
        return {
            "success": False, "error": "LLM returned invalid JSON",
            "raw_output": raw_output, "pipeline": pipeline,
        }
    pipeline.append(parse_step)

    # --- Step 4: write facts ---
    written_facts: list[str] = []
    write_step: dict[str, Any] = {"step": "write_facts", "model": None}
    try:
        for key, value in facts.items():
            await db.fact_set(
                domain,
                key,
                str(value),
                source=f"extracted:{source_id}",
                confidence=0.9,
                origin_type="extracted",
                origin_ref=source_id,
            )
            written_facts.append(key)
        write_step["status"] = "ok"
        write_step["note"] = f"{len(written_facts)} fact(s) written to '{domain}'"
    except Exception as exc:  # noqa: BLE001
        write_step["status"] = "failed"
        write_step["note"] = str(exc)
        pipeline.append(write_step)
        return {"success": False, "error": f"Failed writing facts: {exc}", "pipeline": pipeline}
    pipeline.append(write_step)

    # --- Step 5: embed and store caption as a searchable chunk ---
    if caption:
        cap_step: dict[str, Any] = {"step": "write_caption_chunk", "model": None}
        try:
            sparse_encoder.fit_batch([caption])
            cap_embedding = await embeddings.embed(caption)
            cap_sparse = sparse_encoder.encode(caption)
            now = datetime.now(UTC).isoformat()
            # Upsert a single caption chunk linked to the original source_id.
            # Use a deterministic chunk id so re-running extract overwrites it.
            cap_chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_caption"))
            await vectors.upsert_chunks(
                [{"id": cap_chunk_id, "domain": domain, "source_id": source_id,
                  "source_type": "caption", "source_name": str(source.get("filename") or source_id),
                  "chunk_index": 0, "content": caption, "ingested_at": now}],
                [cap_embedding],
                [cap_sparse],
            )
            # Ensure chunk_count reflects the caption chunk
            if chunk_count == 0:
                await db.source_update_chunk_count(source_id, 1)
            cap_step["status"] = "ok"
            cap_step["note"] = f"{len(caption)} chars embedded and stored"
        except Exception as exc:  # noqa: BLE001
            cap_step["status"] = "failed"
            cap_step["note"] = str(exc)
        pipeline.append(cap_step)

    return {
        "success": True,
        "source_id": source_id,
        "filename": source.get("filename"),
        "domain": domain,
        "model": settings.extraction_model,
        "facts_written": len(written_facts),
        "facts": facts,
        "caption": caption,
        "warnings": warnings,
        "pipeline": pipeline,
    }
