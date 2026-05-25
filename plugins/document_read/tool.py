"""document_read tool — Tika-backed text extraction for document formats.

PDFs, Office docs, RTF and ODF arrive in Hermes as opaque binary blobs.
read_file would either dump FlateDecode-encoded streams (when the
extension is excluded from binary_extensions.py's blocklist, as .pdf is)
or refuse outright (the Office and ODF extensions). This plugin adds an
opt-in tool that PUTs the file body to Apache Tika and returns the
extracted plain text through the same line-numbered / paginated response
shape as read_file — so the agent can use it the way it'd use read_file,
but on documents.

The plugin is wpu-fork-local; it doesn't touch any bundled hermes tool
file. Pure addition.

Why a dedicated tool, not auto-routing inside read_file:
  - PDF extraction has an HTTP roundtrip + possible OCR cost. The agent
    should know it's invoking that, not have it happen by surprise on
    every read_file call with a .pdf path.
  - Failure modes are different (Tika unreachable; image-only scan with
    no OCR layer; format not extractable). A dedicated tool can surface
    them with format-specific guidance.
  - Keeps read_file's contract narrow — text files only.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)


# Apache Tika — "throw any document at it, get text out" service.
# Handles PDF, .doc/.docx, .xls/.xlsx, RTF, HTML uniformly, with Tesseract
# OCR baked in for image-only PDFs. Deployments without TIKA_URL get a
# clear error pointing at terminal pdftotext as a fallback.
_TIKA_TIMEOUT_SECS = float(os.environ.get("TIKA_TIMEOUT", "120"))

# Extensions document_read knows how to extract. PDF is the headline use
# case; the Office and ODF formats come along for free because Tika handles
# them uniformly. Match against `_resolved.suffix.lower()`.
DOCUMENT_EXTENSIONS = frozenset({
    ".pdf",
    ".doc", ".docx",
    ".xls", ".xlsx",
    ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    ".rtf",
})


DOCUMENT_READ_SCHEMA = {
    "name": "document_read",
    "description": (
        "Extract and read text from a document file (PDF, .doc/.docx, "
        ".xls/.xlsx, .ppt/.pptx, RTF, ODF). Hands the file body to "
        "Apache Tika (with Tesseract OCR for image-only PDFs) and "
        "returns the extracted text with line numbers and pagination — "
        "same response shape as read_file. Use this instead of read_file "
        "for any document where read_file would return binary noise or "
        "refuse outright. If extraction yields no text (image-only scan "
        "with no OCR layer) the response says so explicitly; try "
        "vision_analyze on a screenshot of the page in that case."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the document file (absolute, relative, or ~/path)"},
            "offset": {"type": "integer", "description": "Line number (in the extracted text) to start reading from (1-indexed, default: 1)", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 500, max: 2000)", "default": 500, "maximum": 2000},
        },
        "required": ["path"],
    },
}


def extract_via_tika(path: str) -> Optional[str]:
    """PUT a file body to Tika's /tika endpoint and return the extracted
    plain text. Returns None on any failure (URL unset, server
    unreachable, HTTP error, decode error) — caller decides what error
    message to surface to the model. Tika sniffs the body for MIME
    detection so we don't need a format-specific Content-Type."""
    tika_url = os.environ.get("TIKA_URL", "").rstrip("/")
    if not tika_url:
        return None
    try:
        with open(path, "rb") as fh:
            body = fh.read()
    except OSError as e:
        logger.debug("Tika: read of source file failed: %s", e)
        return None
    try:
        # Stdlib only — no hard dep on httpx/requests in this plugin.
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"{tika_url}/tika",
            data=body,
            method="PUT",
            headers={
                "Accept": "text/plain; charset=UTF-8",
                "Content-Type": "application/octet-stream",
            },
        )
        with urllib.request.urlopen(req, timeout=_TIKA_TIMEOUT_SECS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.debug("Tika extraction failed for %s: %s", path, e)
        return None


def document_read_tool(path: str, offset: int = 1, limit: int = 500,
                       task_id: str = "default") -> str:
    """Extract text from a document file via Apache Tika and return it
    through the same line-numbered / paginated response shape as
    read_file. Routes the file body through Tika so PDFs (incl. scans
    via Tesseract OCR), Office docs, and ODF docs become readable text;
    callers don't need to know which extractor handled which format."""
    # Imports here (not at module-top) so the plugin module itself stays
    # cheap to import and tests can monkeypatch these symbols on the
    # plugin module without round-tripping through the bundled file_tools.
    from tools.file_operations import normalize_read_pagination
    from tools.file_tools import (
        _get_file_ops,
        _get_max_read_chars,
        _resolve_path_for_task,
    )
    from agent.file_safety import get_read_block_error
    from agent.redact import redact_sensitive_text

    try:
        offset, limit = normalize_read_pagination(offset, limit)
        _resolved = _resolve_path_for_task(path, task_id)

        if not _resolved.exists():
            return json.dumps({
                "error": f"document_read: file not found: '{path}'",
            }, ensure_ascii=False)

        ext = _resolved.suffix.lower()
        if ext not in DOCUMENT_EXTENSIONS:
            return json.dumps({
                "error": (
                    f"document_read: '{path}' has extension {ext!r} which "
                    "isn't a known document format. Supported: "
                    f"{sorted(DOCUMENT_EXTENSIONS)}. For plain text use "
                    "read_file; for images use vision_analyze."
                ),
            }, ensure_ascii=False)

        block_error = get_read_block_error(path)
        if block_error:
            return json.dumps({"error": block_error})

        text = extract_via_tika(str(_resolved))
        if text is None:
            return json.dumps({
                "error": (
                    f"document_read: text extraction failed for '{path}'. "
                    "TIKA_URL is unset or the Tika server is unreachable. "
                    f"Fallback for PDFs: terminal `pdftotext '{_resolved}' -` "
                    "(poppler-utils is in the base image)."
                ),
            }, ensure_ascii=False)
        if not text.strip():
            return json.dumps({
                "error": (
                    f"document_read: '{path}' produced no extractable text. "
                    "Most likely an image-only scan with no OCR layer. "
                    "Try vision_analyze on a screenshot of the page you "
                    "need, or terminal `tesseract` on a per-page render."
                ),
            }, ensure_ascii=False)

        # Pipe the extracted text through file_ops.read_file so the response
        # shape matches read_file (line numbers, pagination, char-count
        # guard, `truncated`/`total_lines` fields). The tempfile path is
        # bounded to this call and unlinked in `finally`; it never reaches
        # the caller.
        file_ops = _get_file_ops(task_id)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        ) as tf:
            tf.write(text)
            tmp_path = tf.name
        try:
            result = file_ops.read_file(tmp_path, offset, limit)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Char-count guard — same threshold as read_file.
        content_len = len(result.content or "")
        max_chars = _get_max_read_chars()
        result_dict = result.to_dict()
        if content_len > max_chars:
            total_lines = result_dict.get("total_lines", "unknown")
            return json.dumps({
                "error": (
                    f"document_read produced {content_len:,} characters "
                    f"which exceeds the safety limit ({max_chars:,} chars). "
                    "Use offset and limit to read a narrower range. "
                    f"The extracted document has {total_lines} lines total."
                ),
                "path": path,
                "total_lines": total_lines,
            }, ensure_ascii=False)

        # Redact secrets; overwrite path-related fields with the ORIGINAL
        # document's identifiers so the caller never sees the tempfile path.
        if result.content:
            result_dict["content"] = redact_sensitive_text(
                result.content, code_file=False)
        try:
            result_dict["file_size"] = os.path.getsize(_resolved)
        except OSError:
            pass
        result_dict["source_format"] = ext.lstrip(".")
        result_dict["source_path"] = path
        result_dict["extraction"] = "tika"
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        logger.exception("document_read failed for %s", path)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def handle_document_read(args, **kw):
    """Tool dispatcher entrypoint — bridges args dict → document_read_tool."""
    tid = kw.get("task_id") or "default"
    return document_read_tool(
        path=args.get("path", ""),
        offset=args.get("offset", 1),
        limit=args.get("limit", 500),
        task_id=tid,
    )


def check_document_read_available() -> bool:
    """Tool is always advertised; runtime failure modes (TIKA_URL unset,
    server unreachable) surface as clear error messages in the response.
    Hiding the tool when TIKA_URL is missing would make the deployment
    config invisible to the agent — better to let it try and get told."""
    return True
