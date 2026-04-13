"""Tests for PDF/image rendering to base64."""

import base64

from app.pipeline.pdf_renderer import (
    _is_image,
    _is_pdf,
    page_count,
    render_document_pages,
)


def _make_minimal_pdf(num_pages: int = 1) -> bytes:
    """Create a minimal valid PDF with the given number of pages using PyMuPDF."""
    import fitz

    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=200, height=100)
        # Draw some text so the page isn't blank
        page.insert_text((10, 50), f"Page {i + 1}", fontsize=20)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class TestContentTypeDetection:
    def test_pdf_by_content_type(self):
        assert _is_pdf("application/pdf", b"") is True

    def test_pdf_by_magic_bytes(self):
        assert _is_pdf("application/octet-stream", b"%PDF-1.4 rest") is True

    def test_not_pdf(self):
        assert _is_pdf("image/jpeg", b"\xff\xd8\xff") is False

    def test_image_jpeg(self):
        assert _is_image("image/jpeg") is True

    def test_image_png(self):
        assert _is_image("image/png") is True

    def test_not_image(self):
        assert _is_image("application/pdf") is False


class TestRenderPdfPages:
    def test_single_page_pdf(self):
        pdf = _make_minimal_pdf(1)
        images = render_document_pages(pdf, "application/pdf", max_pages=1, dpi=72)
        assert len(images) == 1
        # Verify it's valid base64
        decoded = base64.b64decode(images[0])
        assert len(decoded) > 0
        # JPEG magic bytes
        assert decoded[:2] == b"\xff\xd8"

    def test_multi_page_pdf(self):
        pdf = _make_minimal_pdf(5)
        images = render_document_pages(pdf, "application/pdf", max_pages=3, dpi=72)
        assert len(images) == 3

    def test_max_pages_none_renders_all(self):
        pdf = _make_minimal_pdf(4)
        images = render_document_pages(pdf, "application/pdf", max_pages=None, dpi=72)
        assert len(images) == 4

    def test_max_pages_exceeds_total(self):
        pdf = _make_minimal_pdf(2)
        images = render_document_pages(pdf, "application/pdf", max_pages=10, dpi=72)
        assert len(images) == 2


class TestPageCount:
    def test_pdf_page_count(self):
        pdf = _make_minimal_pdf(3)
        assert page_count(pdf, "application/pdf") == 3

    def test_image_page_count(self):
        # Any image has 1 page
        assert page_count(b"", "image/jpeg") == 1

    def test_unsupported_type(self):
        assert page_count(b"", "text/plain") == 0


class TestUnsupportedType:
    def test_unsupported_content_type_returns_empty(self):
        images = render_document_pages(b"data", "text/plain")
        assert images == []
