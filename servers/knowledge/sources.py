"""Source file scanner, cataloger, and text extraction for the Knowledge pipeline.

Reads synced source files from /opt/mcp-servers/data/sources/ (pushed by
knowledge-push from the laptop). Extracts text from PDFs and text files,
detects image-only PDFs, and maintains a processing manifest.

This module does NOT run vision models — image-based content is flagged
for external processing (e.g., on the laptop via a vision-capable LLM).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from shared.logging_config import get_logger

log = get_logger("knowledge.sources")

# Supported file extensions and their categories
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".tsv"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tiff", ".tif"}
STRUCTURED_EXTENSIONS = {".json", ".yaml", ".yml"}
ALL_SUPPORTED = TEXT_EXTENSIONS | PDF_EXTENSIONS | IMAGE_EXTENSIONS | STRUCTURED_EXTENSIONS

# Files/dirs to skip during scanning
SKIP_NAMES = {".git", ".gitignore", ".extracted", ".sync-manifest.json", "knowledge-push",
              "AGENTS.md", ".DS_Store", "Thumbs.db", "__pycache__"}


class FileCategory(str, Enum):
    """Classification of a source file."""
    TEXT = "text"           # Direct text read (md, txt, csv)
    TEXT_PDF = "text_pdf"   # PDF with extractable text
    IMAGE_PDF = "image_pdf" # PDF that's a scan/image (needs vision)
    IMAGE = "image"         # Image file (needs vision)
    STRUCTURED = "structured"  # JSON/YAML
    UNSUPPORTED = "unsupported"


class ProcessingStatus(str, Enum):
    """Processing state of a source file."""
    UNPROCESSED = "unprocessed"     # Not yet processed
    TEXT_EXTRACTED = "text_extracted"  # Text extracted, ready for fact creation
    NEEDS_VISION = "needs_vision"   # Needs vision model processing
    PROCESSED = "processed"         # Fully processed, facts created
    ERROR = "error"                 # Processing failed


@dataclass
class SourceFile:
    """Metadata and processing state for a single source file."""
    path: str                  # Relative path from sources root
    category: FileCategory
    status: ProcessingStatus = ProcessingStatus.UNPROCESSED
    size_bytes: int = 0
    sha256: str = ""
    text_content: str | None = None
    page_count: int | None = None
    error: str | None = None
    scanned_at: str = ""
    processed_at: str | None = None
    facts_created: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceFile":
        d["category"] = FileCategory(d["category"])
        d["status"] = ProcessingStatus(d["status"])
        return cls(**d)


@dataclass
class SourceManifest:
    """Catalog of all source files and their processing state."""
    files: dict[str, SourceFile] = field(default_factory=dict)  # keyed by relative path
    last_scan: str = ""
    sources_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_scan": self.last_scan,
            "sources_root": self.sources_root,
            "file_count": len(self.files),
            "files": {k: v.to_dict() for k, v in self.files.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceManifest":
        manifest = cls(
            last_scan=d.get("last_scan", ""),
            sources_root=d.get("sources_root", ""),
        )
        for k, v in d.get("files", {}).items():
            manifest.files[k] = SourceFile.from_dict(v)
        return manifest

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> "SourceManifest":
        if not path.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("source_manifest_load_error path=%s error=%s", path, e)
            return cls()


def _sha256(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _classify_file(file_path: Path) -> FileCategory:
    """Classify a source file by extension and content."""
    ext = file_path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return FileCategory.TEXT
    if ext in IMAGE_EXTENSIONS:
        return FileCategory.IMAGE
    if ext in STRUCTURED_EXTENSIONS:
        return FileCategory.STRUCTURED
    if ext in PDF_EXTENSIONS:
        return _classify_pdf(file_path)
    return FileCategory.UNSUPPORTED


def _classify_pdf(file_path: Path) -> FileCategory:
    """Determine if a PDF has extractable text or is image-only."""
    try:
        result = subprocess.run(
            ["pdftotext", str(file_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        text = result.stdout.strip()
        # If pdftotext returns meaningful text (more than whitespace/form feeds)
        cleaned = text.replace("\f", "").strip()
        if len(cleaned) > 20:
            return FileCategory.TEXT_PDF
        return FileCategory.IMAGE_PDF
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return FileCategory.IMAGE_PDF


def _extract_text(file_path: Path, category: FileCategory) -> str | None:
    """Extract text content from a source file. Returns None for image-based files."""
    if category == FileCategory.TEXT:
        try:
            return file_path.read_text(errors="replace")[:100_000]
        except OSError as e:
            log.warning("source_text_read_error path=%s error=%s", file_path, e)
            return None

    if category == FileCategory.TEXT_PDF:
        try:
            result = subprocess.run(
                ["pdftotext", str(file_path), "-"],
                capture_output=True, text=True, timeout=60,
            )
            return result.stdout[:100_000] if result.stdout else None
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("source_pdftotext_error path=%s error=%s", file_path, e)
            return None

    if category == FileCategory.STRUCTURED:
        try:
            return file_path.read_text(errors="replace")[:100_000]
        except OSError:
            return None

    # IMAGE and IMAGE_PDF return None — they need vision processing
    return None


def _pdf_page_count(file_path: Path) -> int | None:
    """Get page count from a PDF using pdfinfo."""
    try:
        result = subprocess.run(
            ["pdfinfo", str(file_path)],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":", 1)[1].strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None


def _should_skip(name: str) -> bool:
    """Check if a file/dir should be skipped during scanning."""
    return name in SKIP_NAMES or name.startswith(".")


def scan_sources(sources_root: Path) -> SourceManifest:
    """Scan the sources directory and build a manifest of all files.

    Preserves processing state for files that haven't changed (same sha256).
    """
    if not sources_root.is_dir():
        log.warning("source_scan_no_dir path=%s", sources_root)
        return SourceManifest(sources_root=str(sources_root))

    # Load existing manifest to preserve state
    manifest_path = sources_root / ".extracted" / "manifest.json"
    existing = SourceManifest.load(manifest_path)

    manifest = SourceManifest(
        sources_root=str(sources_root),
        last_scan=datetime.now(UTC).isoformat(),
    )

    for file_path in sorted(sources_root.rglob("*")):
        if not file_path.is_file():
            continue

        # Skip hidden/system files
        if any(_should_skip(part) for part in file_path.relative_to(sources_root).parts):
            continue

        rel_path = str(file_path.relative_to(sources_root))
        ext = file_path.suffix.lower()

        if ext not in ALL_SUPPORTED:
            continue

        file_hash = _sha256(file_path)
        category = _classify_file(file_path)

        # Reuse existing state if file hasn't changed
        if rel_path in existing.files and existing.files[rel_path].sha256 == file_hash:
            manifest.files[rel_path] = existing.files[rel_path]
            manifest.files[rel_path].scanned_at = manifest.last_scan
            continue

        # New or changed file
        source = SourceFile(
            path=rel_path,
            category=category,
            size_bytes=file_path.stat().st_size,
            sha256=file_hash,
            scanned_at=manifest.last_scan,
        )

        if ext in PDF_EXTENSIONS:
            source.page_count = _pdf_page_count(file_path)

        manifest.files[rel_path] = source

    log.info(
        "source_scan_complete root=%s total=%d new=%d",
        sources_root,
        len(manifest.files),
        sum(1 for f in manifest.files.values() if f.status == ProcessingStatus.UNPROCESSED),
    )

    return manifest


def extract_text_from_sources(
    sources_root: Path,
    manifest: SourceManifest,
    *,
    paths: list[str] | None = None,
) -> SourceManifest:
    """Extract text from source files in the manifest.

    Args:
        sources_root: Root directory of source files.
        manifest: Current source manifest.
        paths: If provided, only process these specific paths. Otherwise process all unprocessed.

    Returns:
        Updated manifest with extraction results.
    """
    targets = paths or [
        p for p, f in manifest.files.items()
        if f.status == ProcessingStatus.UNPROCESSED
    ]

    extracted = 0
    needs_vision = 0
    errors = 0

    for rel_path in targets:
        if rel_path not in manifest.files:
            log.warning("source_extract_not_found path=%s", rel_path)
            continue

        source = manifest.files[rel_path]
        file_path = sources_root / rel_path

        if not file_path.is_file():
            source.status = ProcessingStatus.ERROR
            source.error = "file not found on disk"
            errors += 1
            continue

        if source.category in (FileCategory.IMAGE, FileCategory.IMAGE_PDF):
            source.status = ProcessingStatus.NEEDS_VISION
            needs_vision += 1
            log.info("source_needs_vision path=%s category=%s", rel_path, source.category.value)
            continue

        try:
            text = _extract_text(file_path, source.category)
            if text:
                source.text_content = text
                source.status = ProcessingStatus.TEXT_EXTRACTED
                source.processed_at = datetime.now(UTC).isoformat()
                extracted += 1
                log.info(
                    "source_text_extracted path=%s chars=%d", rel_path, len(text)
                )
            else:
                source.status = ProcessingStatus.NEEDS_VISION
                needs_vision += 1
        except Exception as e:
            source.status = ProcessingStatus.ERROR
            source.error = str(e)
            errors += 1
            log.warning("source_extract_error path=%s error=%s", rel_path, e)

    log.info(
        "source_extraction_complete extracted=%d needs_vision=%d errors=%d",
        extracted, needs_vision, errors,
    )

    return manifest


def convert_pdf_to_images(
    sources_root: Path,
    rel_path: str,
    *,
    dpi: int = 200,
    fmt: str = "jpeg",
) -> list[Path]:
    """Convert a PDF to images using pdftoppm.

    Returns list of generated image paths in .extracted/ directory.
    """
    file_path = sources_root / rel_path
    if not file_path.is_file():
        return []

    output_dir = sources_root / ".extracted" / "images" / Path(rel_path).stem
    output_dir.mkdir(parents=True, exist_ok=True)

    prefix = str(output_dir / "page")
    ext_flag = f"-{fmt}"

    try:
        subprocess.run(
            ["pdftoppm", ext_flag, f"-r", str(dpi), str(file_path), prefix],
            capture_output=True, text=True, timeout=120,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("source_pdf_to_image_error path=%s error=%s", rel_path, e)
        return []

    # pdftoppm names files like page-1.jpg, page-2.jpg, etc.
    suffix = ".jpg" if fmt == "jpeg" else f".{fmt}"
    images = sorted(output_dir.glob(f"page-*{suffix}"))
    log.info("source_pdf_to_images path=%s pages=%d output_dir=%s", rel_path, len(images), output_dir)
    return images


def get_source_summary(manifest: SourceManifest) -> dict[str, Any]:
    """Return a summary of source files by status and category."""
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_dir: dict[str, int] = {}

    for source in manifest.files.values():
        by_status[source.status.value] = by_status.get(source.status.value, 0) + 1
        by_category[source.category.value] = by_category.get(source.category.value, 0) + 1

        # Group by top-level directory
        parts = Path(source.path).parts
        top_dir = parts[0] if len(parts) > 1 else "(root)"
        by_dir[top_dir] = by_dir.get(top_dir, 0) + 1

    return {
        "total_files": len(manifest.files),
        "last_scan": manifest.last_scan,
        "by_status": by_status,
        "by_category": by_category,
        "by_directory": by_dir,
    }
