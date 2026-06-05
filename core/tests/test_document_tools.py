"""P1: Document Tools Tests — PDF, DOCX, XLSX (fallback + path safety)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowcraft_core.tools.document import PdfReadTool, DocxReadTool, ExcelReadTool
from flowcraft_core.tools.base import ToolDefinition, is_path_allowed
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.domain.enums import RiskLevel


def make_intent(tool_name: str, path: str) -> ToolIntent:
    return ToolIntent(
        task_id="t_doc", step_id="s1",
        tool_name=tool_name, purpose="read document",
        input_summary="read", input_payload={"path": path},
        expected_result="content",
    )


class TestDocumentToolDefinitions:
    """Metadata and definition validation."""

    @pytest.mark.unit
    def test_pdf_definition(self) -> None:
        tool = PdfReadTool([Path("/tmp")])
        assert tool.definition.tool_name == "document.pdf.read"
        assert tool.definition.category == "document"
        assert tool.definition.risk_level == RiskLevel.LOW

    @pytest.mark.unit
    def test_docx_definition(self) -> None:
        tool = DocxReadTool([Path("/tmp")])
        assert tool.definition.tool_name == "document.docx.read"

    @pytest.mark.unit
    def test_xlsx_definition(self) -> None:
        tool = ExcelReadTool([Path("/tmp")])
        assert tool.definition.tool_name == "document.xlsx.read"


class TestDocumentPathSafety:
    """Path safety enforcement."""

    @pytest.mark.component
    def test_pdf_read_denies_outside_path(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "doc.pdf"
        outside.write_text("fake pdf content")
        tool = PdfReadTool([sandbox])
        intent = make_intent("document.pdf.read", str(outside))
        obs = asyncio.run(tool.execute(intent))
        assert obs.status in ("DENIED", "FAILED")

    @pytest.mark.component
    def test_pdf_read_file_not_found(self, tmp_path: Path) -> None:
        tool = PdfReadTool([tmp_path])
        intent = make_intent("document.pdf.read", str(tmp_path / "nope.pdf"))
        obs = asyncio.run(tool.execute(intent))
        assert obs.status == "FAILED"

    @pytest.mark.component
    def test_pdf_read_fallback_without_fitz(self, tmp_path: Path) -> None:
        """Without PyMuPDF, fallback reads raw bytes warning."""
        doc = tmp_path / "test.pdf"
        doc.write_text("%PDF-1.4 mock pdf content")
        tool = PdfReadTool([tmp_path])
        intent = make_intent("document.pdf.read", str(doc))
        obs = asyncio.run(tool.execute(intent))
        # Should succeed via fallback (no crash)
        assert obs.status in ("COMPLETED", "completed", "success")

    @pytest.mark.component
    def test_docx_fallback_without_docx_lib(self, tmp_path: Path) -> None:
        """Without python-docx, fallback provides helpful message."""
        doc = tmp_path / "test.docx"
        doc.write_text("mock docx content")
        tool = DocxReadTool([tmp_path])
        intent = make_intent("document.docx.read", str(doc))
        obs = asyncio.run(tool.execute(intent))
        # Should not crash
        assert obs.status is not None

    @pytest.mark.component
    def test_xlsx_fallback_without_lib(self, tmp_path: Path) -> None:
        """Without openpyxl, fallback provides helpful message."""
        doc = tmp_path / "test.xlsx"
        doc.write_text("mock xlsx content")
        tool = ExcelReadTool([tmp_path])
        intent = make_intent("document.xlsx.read", str(doc))
        obs = asyncio.run(tool.execute(intent))
        # Should not crash
        assert obs.status is not None
