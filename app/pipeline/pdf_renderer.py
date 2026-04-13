"""Render document files (PDF/image) to base64-encoded images for vision models."""

from __future__ import annotations

import base64

import structlog

log = structlog.get_logger(__name__)


def render_document_pages(
    file_bytes: bytes,
    content_type: str,
    *,
    max_pages: int | None = None,
    dpi: int = 150,
) -> list[str]:
    """Convert a document file to a list of base64-encoded JPEG images.

    For PDFs, renders up to *max_pages* pages at the given *dpi*.
    For images (JPEG/PNG/TIFF), returns a single-element list.
    Returns base64 strings (no data URI prefix) suitable for Ollama's ``images`` field.
    """
    if _is_pdf(content_type, file_bytes):
        return _render_pdf_pages(file_bytes, max_pages=max_pages, dpi=dpi)
    if _is_image(content_type):
        return [_render_image(file_bytes)]
    log.warning("unsupported content type for vision OCR", content_type=content_type)
    return []


def _is_pdf(content_type: str, file_bytes: bytes) -> bool:
    return content_type.startswith("application/pdf") or file_bytes[:5] == b"%PDF-"


def _is_image(content_type: str) -> bool:
    return content_type.startswith("image/")


def _render_pdf_pages(
    pdf_bytes: bytes,
    *,
    max_pages: int | None = None,
    dpi: int = 150,
) -> list[str]:
    """Render PDF pages to base64-encoded JPEG images."""
    import fitz  # PyMuPDF — imported lazily so the dep is optional at import time

    images: list[str] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total_pages = doc.page_count
        render_count = min(total_pages, max_pages) if max_pages else total_pages
        for page_idx in range(render_count):
            page = doc[page_idx]
            # Scale from default 72 DPI to target DPI
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            # Convert to JPEG bytes
            jpeg_bytes = pix.tobytes("jpeg")
            images.append(base64.b64encode(jpeg_bytes).decode("ascii"))
            log.debug(
                "rendered pdf page",
                page=page_idx + 1,
                width=pix.width,
                height=pix.height,
                size_kb=len(jpeg_bytes) // 1024,
            )
    finally:
        doc.close()

    log.info("rendered pdf pages", total_pages=total_pages, rendered=len(images), dpi=dpi)
    return images


def _render_image(image_bytes: bytes) -> str:
    """Resize an image if needed and return as base64-encoded JPEG."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=image_bytes)
    try:
        page = doc[0]
        pix = page.get_pixmap()
        # Convert to JPEG
        jpeg_bytes = pix.tobytes("jpeg")
        return base64.b64encode(jpeg_bytes).decode("ascii")
    finally:
        doc.close()


def page_count(file_bytes: bytes, content_type: str) -> int:
    """Return the number of pages in a document file."""
    if _is_pdf(content_type, file_bytes):
        import fitz

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            return doc.page_count
        finally:
            doc.close()
    if _is_image(content_type):
        return 1
    return 0
