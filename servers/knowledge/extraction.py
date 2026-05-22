"""Text extraction, OCR, vision, and chunking for the Knowledge service.

Extracted from knowledge_server.py during Phase 3 modularization.
Contains document processing pipeline: PDF extraction, image OCR,
text chunking, and binary detection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from servers.knowledge.settings import KnowledgeSettings
from shared.logging_config import get_logger

log = get_logger("knowledge")


# ---------------------------------------------------------------------------
# Document Processing
# ---------------------------------------------------------------------------


def compute_file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def source_download_bytes(
    settings: KnowledgeSettings,
    db: KnowledgeDB,
    source_id: str,
    vectors: KnowledgeVectorStore | None = None,
) -> dict[str, Any]:
    """Return original source bytes for a stored source."""
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    filename = sanitize_source_filename(str(source.get("filename") or f"{source_id}.bin"))
    source_path = resolve_source_path(settings.knowledge_path, source)
    if source_path:
        data = source_path.read_bytes()
        media_type = source.get("media_type") or source_media_type(filename)
        generated = False
    elif vectors:
        export = await source_chunk_export_bytes(vectors, source)
        if not export:
            return {
                "success": False,
                "error": f"Stored source file for '{source_id}' was not found",
            }
        filename, data = export
        media_type = "text/markdown"
        generated = True
    else:
        return {
            "success": False,
            "error": f"Stored source file for '{source_id}' was not found",
        }

    return {
        "success": True,
        "source_id": source_id,
        "filename": filename,
        "domain": source.get("domain"),
        "media_type": media_type,
        "size_bytes": len(data),
        "generated": generated,
        "data": data,
    }


async def delete_source_record(
    settings: KnowledgeSettings,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    source_id: str,
    delete_file: bool = True,
) -> dict[str, Any]:
    """Delete one source row, its vector chunks, and optionally its stored file."""
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    await vectors.delete_by_source(source_id)
    deleted_files: list[str] = []
    preserved_files: list[str] = []
    if delete_file:
        candidate = resolve_source_path(settings.knowledge_path, source)
        if candidate:
            rel_path = source_relative_path(settings.knowledge_path, candidate)
            references = await db.sources_referencing_file(
                stored_paths=[rel_path, str(candidate)],
                domain=source.get("domain"),
                filename=source.get("filename"),
                exclude_source_id=source_id,
            )
            if references:
                preserved_files.append(rel_path)
            else:
                candidate.unlink()
                deleted_files.append(rel_path)

    deleted = await db.source_remove(source_id)
    return {
        "success": deleted,
        "deleted": deleted,
        "source": source,
        "deleted_files": deleted_files,
        "preserved_files": preserved_files,
    }


async def delete_sources_for_overwrite(
    settings: KnowledgeSettings,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    domain: str,
    filename: str,
) -> list[dict[str, Any]]:
    """Remove existing source rows/chunks for a domain filename before replacement."""
    deleted: list[dict[str, Any]] = []
    for source in await db.sources_list_by_domain_filename(domain, filename):
        result = await delete_source_record(
            settings,
            vectors,
            db,
            str(source["id"]),
            delete_file=True,
        )
        if result.get("success"):
            deleted.append(result)
    return deleted


async def rename_source_record(
    settings: KnowledgeSettings,
    vectors: KnowledgeVectorStore,
    db: KnowledgeDB,
    source_id: str,
    filename: str,
) -> dict[str, Any]:
    """Rename a source for display/search and rename raw bytes when present."""
    source = await db.source_get(source_id)
    if not source:
        return {"success": False, "error": f"Source '{source_id}' not found"}

    clean_filename = sanitize_source_filename(filename)
    if not clean_filename:
        return {"success": False, "error": "filename is required"}

    old_path = resolve_source_path(settings.knowledge_path, source)
    renamed_file = False
    preserved_files: list[str] = []
    stored_path = source.get("stored_path")
    if old_path and old_path.exists() and old_path.is_file():
        new_path = old_path.with_name(clean_filename)
        rel_path = source_relative_path(settings.knowledge_path, old_path)
        if new_path != old_path:
            references = await db.sources_referencing_file(
                stored_paths=[rel_path, str(old_path)],
                domain=source.get("domain"),
                filename=source.get("filename"),
                exclude_source_id=source_id,
            )
            if references:
                preserved_files.append(rel_path)
                stored_path = rel_path
            else:
                if new_path.exists():
                    return {"success": False, "error": f"File already exists: {new_path.name}"}
                old_path.rename(new_path)
                renamed_file = True
                stored_path = source_relative_path(settings.knowledge_path, new_path)
        else:
            stored_path = rel_path

    await db.source_rename(source_id, clean_filename, stored_path)
    await vectors.update_source_name(source_id, clean_filename)
    updated = await db.source_get(source_id)
    return {
        "success": True,
        "source_id": source_id,
        "old_filename": source.get("filename"),
        "new_filename": clean_filename,
        "renamed_file": renamed_file,
        "preserved_files": preserved_files,
        "source": updated,
    }


def compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
}

# Common binary file magic byte prefixes used to detect binary files with no extension.
_BINARY_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x89PNG",          # PNG
    b"\xff\xd8",        # JPEG
    b"GIF8",            # GIF
    b"BM",              # BMP
    b"RIFF",            # WebP / WAV
    b"\x49\x49\x2a\x00",  # TIFF little-endian
    b"\x4d\x4d\x00\x2a",  # TIFF big-endian
    b"PK\x03\x04",     # ZIP / DOCX / XLSX
    b"\x1f\x8b",       # GZIP
    b"\x7fELF",        # ELF binary
    b"ID3",            # MP3 ID3 tag
    b"\xff\xfb",       # MP3 frame sync
    b"\x4f\x67\x67\x53",  # OGG
)


def _is_likely_binary(raw: bytes) -> bool:
    """Return True when raw bytes look like a binary/non-text file."""
    head = raw[:16]
    for magic in _BINARY_MAGIC_PREFIXES:
        if head.startswith(magic):
            return True
    # ISO base media file format (HEIC, HEIF, MP4): 'ftyp' at bytes 4-8
    if len(raw) >= 8 and raw[4:8] == b"ftyp":
        return True
    # Null byte: almost never appears in UTF-8 text
    if b"\x00" in raw[:512]:
        return True
    # High ratio of control characters
    sample = raw[:512]
    control = sum(1 for b in sample if b < 0x09 or b in (0x0b, 0x0c) or 0x0E <= b <= 0x1F)
    return bool(sample) and control / len(sample) > 0.10
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json", ".yaml", ".yml",
    ".html", ".htm", ".xml",
}


async def _run(
    cmd: list[str],
    stdin: bytes | None = None,
    timeout: float = 120.0,
) -> tuple[int, bytes, bytes]:
    """Run a subprocess and return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", b"timeout"
    return proc.returncode or 0, out, err


VISION_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text visible in this image VERBATIM. "
    "Preserve spelling, numbers, punctuation, and order. Include every label, field, "
    "barcode value, and stamp. For tables, output rows as plain text with columns "
    "separated by ' | '. Do not summarize, do not translate, do not redact, do not "
    "invent text that is not visible. Output only the transcribed text — no preamble "
    "or commentary. If the image contains no text, output exactly: [no text]"
)

IMAGE_DESCRIPTION_PROMPT = (
    "Describe this image in 2-4 sentences for a personal knowledge base. "
    "Cover the main subject, setting, notable people (no names needed), "
    "any visible text, and specific details that would help someone find this "
    "image when searching. Be concrete and factual. Output only the description."
)

EXTRACTION_SYSTEM_PROMPT = (
    "You are a document extraction engine for a personal knowledge base.\n"
    "Your job: read the provided document content and return a JSON object.\n\n"
    "Rules:\n"
    "- Extract every value you can see. Do not fabricate, guess, or paraphrase values.\n"
    "- Use stable snake_case keys with meaningful prefixes, e.g. w2_2025_box1_wages, "
    "passport_us_number, lab_ldl_2024_12.\n"
    "- For dates use ISO format: YYYY-MM-DD.\n"
    "- For currency include the number only (no $ sign): 94200.00\n"
    "- For images with no document structure (photos, pets, scenery): set 'caption' "
    "to a 2-3 sentence description and set 'facts' to {}; do not create facts for "
    "visible clothing, background objects, sky, seating, hair, or similar photo details.\n"
    "- For documents: set 'facts' to all extracted key/value pairs, set 'caption' to null.\n"
    "- Omit fields that are not legible or not present — do not set null or 'unknown'.\n"
    "- Put brief uncertainty notes in 'warnings', e.g. unreadable or partially obscured fields.\n"
    "- Output only valid JSON. No markdown fences, no commentary.\n"
    "Output format: {\"facts\": {\"key\": \"value\", ...}, \"caption\": null, \"warnings\": []}"
)


def _decode_llm_json_object(raw_output: str) -> dict[str, Any]:
    clean = raw_output.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean.rstrip())
    if not clean.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", clean)
        if match:
            clean = match.group(0)
    decoded, _ = json.JSONDecoder().raw_decode(clean)
    if not isinstance(decoded, dict):
        raise ValueError("JSON root must be an object")
    return decoded


async def _vision_ocr_bytes(
    image_bytes: bytes, media_type: str, settings: KnowledgeSettings
) -> str:
    """OCR an image via OpenRouter vision LLM. Returns text or empty on failure."""
    if not settings.vision_model or not settings.openrouter_api_key:
        return ""

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{media_type};base64,{b64}"
    payload = {
        "model": settings.vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            return "" if text == "[no text]" else text
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vision_ocr_failed model=%s error=%r", settings.vision_model, exc,
        )
        return ""


async def _tesseract_image(path: Path, language: str) -> str:
    rc, out, _ = await _run(["tesseract", str(path), "-", "-l", language])
    return out.decode("utf-8", errors="replace") if rc == 0 else ""


_IMAGE_MEDIA = {
    ".avif": "image/avif",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".heic": "image/heic", ".heif": "image/heif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


async def _ocr_image_file(path: Path, settings: KnowledgeSettings) -> str:
    """Vision LLM first, tesseract fallback."""
    if settings.vision_model and settings.openrouter_api_key:
        try:
            data = path.read_bytes()
            media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
            text = await _vision_ocr_bytes(data, media, settings)
            if text:
                return text
        except OSError:
            pass
    return await _tesseract_image(path, settings.ocr_language)


async def _extract_pdf_text(path: Path, settings: KnowledgeSettings) -> str:
    """pdftotext for native PDFs; rasterize + vision LLM for scans."""
    rc, out, _ = await _run(["pdftotext", "-layout", str(path), "-"])
    text = out.decode("utf-8", errors="replace").strip() if rc == 0 else ""
    if text:
        return text

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        rc, _, _ = await _run([
            "pdftoppm", "-r", str(settings.vision_dpi), "-png",
            str(path), str(prefix),
        ])
        if rc != 0:
            return ""
        page_files = sorted(Path(tmp).glob("page-*.png"))[: settings.vision_max_pages]
        pages: list[str] = []
        for img in page_files:
            pages.append(await _ocr_image_file(img, settings))
        return "\n\n".join(p for p in pages if p.strip())


# ---------------------------------------------------------------------------
# Pipeline-logging extraction functions
# Each returns (text_or_chunks, pipeline_steps) so callers can report exactly
# what ran, which model was called, whether it succeeded or fell back.
# ---------------------------------------------------------------------------


async def _vision_call(
    image_bytes: bytes,
    media_type: str,
    prompt: str,
    model: str,
    api_key: str,
    step_name: str,
) -> tuple[str, dict[str, Any]]:
    """Single OpenRouter vision LLM call. Returns (text, pipeline_step)."""
    step: dict[str, Any] = {
        "step": step_name,
        "model": model,
        "status": "failed",
        "tokens_in": 0,
        "tokens_out": 0,
        "note": "",
    }
    if not model or not api_key:
        step["status"] = "skipped"
        step["note"] = "no model or api_key configured"
        return "", step

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{media_type};base64,{b64}"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage") or {}
            step["tokens_in"] = usage.get("prompt_tokens", 0)
            step["tokens_out"] = usage.get("completion_tokens", 0)
            if text == "[no text]":
                text = ""
                step["status"] = "ok"
                step["note"] = "model reported: no text in image"
            else:
                step["status"] = "ok"
                step["note"] = f"{len(text)} chars"
            return text, step
    except Exception as exc:  # noqa: BLE001
        step["status"] = "failed"
        step["note"] = str(exc)
        log.warning("vision_call failed model=%s step=%s error=%r", model, step_name, exc)
        return "", step


async def _describe_image_file(
    path: Path, settings: KnowledgeSettings
) -> tuple[str, list[dict[str, Any]]]:
    """Describe a photo/image using the vision model. Returns (description, steps)."""
    steps: list[dict[str, Any]] = []
    if not settings.vision_model or not settings.openrouter_api_key:
        steps.append({
            "step": "image_description", "model": None, "status": "skipped",
            "note": "KNOWLEDGE_VISION_MODEL not configured",
        })
        return "", steps
    try:
        data = path.read_bytes()
        media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
    except OSError as exc:
        steps.append({
            "step": "image_description", "model": settings.vision_model,
            "status": "failed", "note": f"read error: {exc}",
        })
        return "", steps
    text, step = await _vision_call(
        data, media, IMAGE_DESCRIPTION_PROMPT,
        settings.vision_model, settings.openrouter_api_key, "image_description",
    )
    steps.append(step)
    return text, steps


async def _ocr_image_file_with_log(
    path: Path, settings: KnowledgeSettings
) -> tuple[str, list[dict[str, Any]]]:
    """Vision LLM OCR, tesseract fallback. Returns (text, steps)."""
    steps: list[dict[str, Any]] = []
    if settings.vision_model and settings.openrouter_api_key:
        try:
            data = path.read_bytes()
            media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
            text, step = await _vision_call(
                data, media, VISION_OCR_PROMPT,
                settings.vision_model, settings.openrouter_api_key, "vision_ocr",
            )
            steps.append(step)
            if text:
                return text, steps
        except OSError as exc:
            steps.append({
                "step": "vision_ocr", "model": settings.vision_model,
                "status": "failed", "note": f"read error: {exc}",
            })
    # Tesseract fallback
    tess_step: dict[str, Any] = {"step": "tesseract", "model": "tesseract"}
    rc, out, _ = await _run(["tesseract", str(path), "-", "-l", settings.ocr_language])
    if rc == 0:
        text = out.decode("utf-8", errors="replace")
        tess_step["status"] = "ok"
        tess_step["note"] = f"{len(text)} chars (fallback)"
    else:
        text = ""
        tess_step["status"] = "failed"
        tess_step["note"] = "tesseract returned non-zero exit code"
    steps.append(tess_step)
    return text, steps


async def _extract_pdf_text_with_log(
    path: Path, settings: KnowledgeSettings
) -> tuple[str, list[dict[str, Any]]]:
    """pdftotext for native PDFs; rasterize + OCR for scans. Returns (text, steps)."""
    steps: list[dict[str, Any]] = []
    pdf_step: dict[str, Any] = {"step": "pdftotext", "model": None}
    rc, out, _ = await _run(["pdftotext", "-layout", str(path), "-"])
    text = out.decode("utf-8", errors="replace").strip() if rc == 0 else ""
    if text:
        pdf_step["status"] = "ok"
        pdf_step["note"] = f"{len(text)} chars (native PDF text)"
        steps.append(pdf_step)
        return text, steps
    pdf_step["status"] = "ok" if rc == 0 else "failed"
    pdf_step["note"] = "no embedded text — scanned PDF" if rc == 0 else "pdftotext failed"
    steps.append(pdf_step)

    import tempfile
    raster_step: dict[str, Any] = {"step": "rasterize", "model": None}
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        rc, _, _ = await _run([
            "pdftoppm", "-r", str(settings.vision_dpi), "-png", str(path), str(prefix),
        ])
        if rc != 0:
            raster_step["status"] = "failed"
            raster_step["note"] = "pdftoppm failed"
            steps.append(raster_step)
            return "", steps
        page_files = sorted(Path(tmp).glob("page-*.png"))[: settings.vision_max_pages]
        raster_step["status"] = "ok"
        raster_step["note"] = (
            f"{len(page_files)} page(s) rasterized at {settings.vision_dpi} dpi"
        )
        steps.append(raster_step)

        pages: list[str] = []
        for img in page_files:
            page_text, page_steps = await _ocr_image_file_with_log(img, settings)
            for s in page_steps:
                s["page"] = img.name
            steps.extend(page_steps)
            if page_text.strip():
                pages.append(page_text)

    text = "\n\n".join(p for p in pages if p.strip())
    if page_files and len(text) < 100:
        steps.append({
            "step": "confidence_check",
            "model": None,
            "status": "warn",
            "note": (
                f"low OCR output ({len(text)} chars across {len(page_files)} page(s)) "
                "— consider using Extract Facts with Sonnet for better accuracy"
            ),
        })
    return text, steps


async def _extract_and_chunk_with_log(
    path: Path, settings: KnowledgeSettings
) -> tuple[list[str], list[dict[str, Any]], str]:
    """Extract text and split into chunks with a full pipeline log.

    Returns (chunks, pipeline_steps, pipeline_type) where pipeline_type is one of:
    'image_description' | 'document_ocr' | 'text_read' | 'unsupported'
    """
    suffix = path.suffix.lower()
    steps: list[dict[str, Any]] = []
    text = ""

    if suffix == ".pdf":
        pipeline_type = "document_ocr"
        text, steps = await _extract_pdf_text_with_log(path, settings)
    elif suffix in IMAGE_EXTENSIONS and settings.ocr_enabled:
        # Photos/images: generate a semantic description, not verbatim OCR.
        # OCR is reserved for PDFs where text layout matters.
        pipeline_type = "image_description"
        text, steps = await _describe_image_file(path, settings)
    elif suffix in TEXT_EXTENSIONS or suffix == "":
        pipeline_type = "text_read"
        read_step: dict[str, Any] = {"step": "text_read", "model": None}
        try:
            raw = path.read_bytes()
            if suffix == "" and _is_likely_binary(raw):
                read_step["status"] = "skipped"
                read_step["note"] = "binary file with no extension — no text indexing"
                steps.append(read_step)
                return [], steps, "unsupported"
            text = raw.decode("utf-8", errors="replace")
            read_step["status"] = "ok"
            read_step["note"] = f"{len(text)} chars read"
        except OSError as exc:
            read_step["status"] = "failed"
            read_step["note"] = str(exc)
            text = ""
        steps.append(read_step)
    else:
        steps.append({
            "step": "classify", "model": None, "status": "skipped",
            "note": f"unsupported file type: {suffix}",
        })
        return [], steps, "unsupported"

    text = text.strip()
    if not text:
        if not any(s.get("status") in ("failed", "warn") for s in steps):
            steps.append({
                "step": "chunking", "model": None, "status": "skipped",
                "note": "no text extracted — nothing to chunk",
            })
        return [], steps, pipeline_type

    chunks = chunk_text(text, settings.chunk_max_chars, settings.chunk_overlap)
    steps.append({
        "step": "chunking", "model": None, "status": "ok",
        "note": f"{len(chunks)} chunk(s) from {len(text)} chars",
    })
    return chunks, steps, pipeline_type


async def extract_and_chunk(path: Path, settings: KnowledgeSettings) -> list[str]:
    """Extract text from a file and split into chunks.

    Pipeline: pdftotext for native PDFs (free), vision LLM via OpenRouter for
    scanned PDFs and images (accurate), tesseract as final fallback.
    """
    suffix = path.suffix.lower()
    text = ""

    if suffix == ".pdf":
        text = await _extract_pdf_text(path, settings)
    elif suffix in IMAGE_EXTENSIONS and settings.ocr_enabled:
        text = await _ocr_image_file(path, settings)
    elif suffix in TEXT_EXTENSIONS or suffix == "":
        try:
            raw = path.read_bytes()
            if suffix == "" and _is_likely_binary(raw):
                # Extensionless binary file (e.g. image uploaded without extension).
                # Store the bytes but skip text indexing; caption via knowledge_ingest_text.
                return []
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            text = ""
    else:
        # Unknown binary type — store the file but skip indexing.
        return []

    text = text.strip()
    if not text:
        return []

    return chunk_text(text, settings.chunk_max_chars, settings.chunk_overlap)


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 200) -> list[str]:
    """Chunk plain text into overlapping segments."""
    text = text.strip()
    if not text:
        return []

    max_chars = max(1, int(max_chars or 1000))
    overlap = max(0, min(int(overlap or 0), max_chars - 1))
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""

    def append_current() -> None:
        nonlocal current
        clean = current.strip()
        if clean:
            chunks.append(clean)
        current = ""

    def append_long_segment(segment: str) -> None:
        start = 0
        while start < len(segment):
            end = min(start + max_chars, len(segment))
            piece = segment[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(segment):
                break
            start = end - overlap if overlap else end

    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue

        if len(para) > max_chars:
            append_current()
            append_long_segment(para)
            continue

        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(para) <= max_chars:
            current = f"{current}{separator}{para}" if current else para
        else:
            append_current()
            if chunks and overlap > 0:
                prefix = chunks[-1][-overlap:].strip()
                candidate = f"{prefix}\n\n{para}" if prefix else para
                current = candidate if len(candidate) <= max_chars else para
            else:
                current = para
    append_current()

    return [c for c in chunks if c]


