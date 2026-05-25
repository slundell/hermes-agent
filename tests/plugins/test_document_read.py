"""Tests for the document_read plugin — Tika-backed document text extraction.

Verifies the tool surface (schema + handler), the happy path (Tika returns
text, response shape matches read_file), and the documented failure modes
(Tika unreachable; image-only scan; unsupported extension; missing file).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from plugins.document_read.tool import (
    DOCUMENT_EXTENSIONS,
    DOCUMENT_READ_SCHEMA,
    check_document_read_available,
    document_read_tool,
)


class TestDocumentReadSchema:
    def test_required_path_param(self):
        assert DOCUMENT_READ_SCHEMA["name"] == "document_read"
        assert DOCUMENT_READ_SCHEMA["parameters"]["required"] == ["path"]
        props = DOCUMENT_READ_SCHEMA["parameters"]["properties"]
        assert props["offset"].get("default") == 1
        assert props["limit"].get("default") == 500

    def test_description_signals_tika_and_pdf(self):
        desc = DOCUMENT_READ_SCHEMA["description"]
        assert "Tika" in desc
        assert "PDF" in desc

    def test_supported_extensions_cover_documents(self):
        # PDF is the headline use case; Office and ODF come along for free.
        for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".rtf"):
            assert ext in DOCUMENT_EXTENSIONS


class TestDocumentReadAvailability:
    def test_always_advertised(self):
        # Tool stays advertised even when TIKA_URL is unset; runtime
        # failures surface as actionable errors, not invisible tool loss.
        assert check_document_read_available() is True


class TestDocumentReadHandler:
    @patch("plugins.document_read.tool.extract_via_tika")
    @patch("tools.file_tools._get_file_ops")
    def test_happy_path_returns_extracted_text(
            self, mock_get, mock_tika, tmp_path):
        pdf = tmp_path / "interview.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")  # real file so .exists() passes
        mock_tika.return_value = (
            "WITNESS: I saw a man at the corner.\n"
            "OFFICER: At what time?\n"
            "WITNESS: Around 23:20.\n"
        )
        # file_ops.read_file is what does the line-numbering; mock it.
        mock_ops = MagicMock()
        result_obj = MagicMock()
        result_obj.content = "1|WITNESS:..."
        result_obj.to_dict.return_value = {
            "content": "1|WITNESS:...", "total_lines": 3,
            "file_size": 64, "truncated": False,
        }
        mock_ops.read_file.return_value = result_obj
        mock_get.return_value = mock_ops

        r = json.loads(document_read_tool(str(pdf)))
        assert r["source_format"] == "pdf"
        assert r["source_path"] == str(pdf)
        assert r["extraction"] == "tika"
        # file_size must reflect the PDF's real size, not the tempfile's.
        assert r["file_size"] == pdf.stat().st_size
        mock_tika.assert_called_once()

    @patch("plugins.document_read.tool.extract_via_tika")
    def test_tika_unreachable_surfaces_actionable_error(
            self, mock_tika, tmp_path):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        mock_tika.return_value = None

        r = json.loads(document_read_tool(str(pdf)))
        err = r.get("error", "")
        assert "Tika" in err or "TIKA_URL" in err
        # Points the agent at a concrete fallback so it has a next step.
        assert "pdftotext" in err

    @patch("plugins.document_read.tool.extract_via_tika")
    def test_empty_extraction_explained(self, mock_tika, tmp_path):
        # Image-only scan with no OCR layer — Tika returns empty/whitespace.
        pdf = tmp_path / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        mock_tika.return_value = "   \n  \n"

        r = json.loads(document_read_tool(str(pdf)))
        err = r.get("error", "")
        assert "no extractable text" in err
        # The error suggests an OCR fallback so the agent isn't stuck.
        assert any(s in err for s in ("OCR", "vision_analyze", "tesseract"))

    def test_unsupported_extension_rejected(self, tmp_path):
        # .png is not a document — refuse rather than ship it to Tika and
        # muddy the tool's contract.
        png = tmp_path / "x.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        r = json.loads(document_read_tool(str(png)))
        err = r.get("error", "")
        assert "isn't a known document format" in err or ".pdf" in err

    def test_missing_file_returns_not_found(self, tmp_path):
        r = json.loads(document_read_tool(str(tmp_path / "nope.pdf")))
        assert "not found" in r.get("error", "")

    @patch("plugins.document_read.tool.extract_via_tika")
    @patch("tools.file_tools._get_file_ops")
    @patch("plugins.document_read.tool.__name__")
    def test_handles_doc_docx_xlsx_uniformly(
            self, mock_name, mock_get, mock_tika, tmp_path):
        # All supported extensions go through the same Tika path. Spot-check
        # that the handler accepts each rather than treating PDF specially.
        mock_tika.return_value = "Hello document."
        mock_ops = MagicMock()
        result_obj = MagicMock()
        result_obj.content = "1|Hello document."
        result_obj.to_dict.return_value = {
            "content": "1|Hello document.", "total_lines": 1,
            "file_size": 10, "truncated": False,
        }
        mock_ops.read_file.return_value = result_obj
        mock_get.return_value = mock_ops

        for ext in (".docx", ".xlsx", ".odt", ".rtf"):
            doc = tmp_path / f"file{ext}"
            doc.write_bytes(b"\x00\x00\x00\x00")
            r = json.loads(document_read_tool(str(doc)))
            assert r.get("source_format") == ext.lstrip("."), (
                f"{ext} dispatch failed: {r}"
            )


class TestPluginRegistration:
    def test_register_wires_document_read_tool(self):
        from plugins.document_read import register

        class _Ctx:
            def __init__(self):
                self.registered = []

            def register_tool(self, **kwargs):
                self.registered.append(kwargs)

        ctx = _Ctx()
        register(ctx)
        assert len(ctx.registered) == 1
        call = ctx.registered[0]
        assert call["name"] == "document_read"
        assert call["toolset"] == "file"
        assert call["schema"] is DOCUMENT_READ_SCHEMA
        assert callable(call["handler"])
        assert callable(call["check_fn"])
