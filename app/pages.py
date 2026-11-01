"""Render a page of the source PDF as an image for the UI (pypdfium2, BSD/Apache).

The PDF ships in the image (data/source/), so a cited page can be shown next to
the answer and checked by eye. PyMuPDF (AGPL) stays offline-only.
"""

from __future__ import annotations

import atexit
import io
import threading
from functools import lru_cache
from pathlib import Path

import pypdfium2 as pdfium

PDF_PATH = Path(__file__).resolve().parent.parent / "data" / "source" / "iPhone_User_Guide_1.pdf"
_lock = threading.Lock()  # PDFium is not thread-safe; Streamlit serves sessions on threads


@lru_cache(maxsize=1)
def _document() -> pdfium.PdfDocument:
    doc = pdfium.PdfDocument(str(PDF_PATH))
    atexit.register(doc.close)
    return doc


def page_count() -> int:
    with _lock:
        return len(_document())


@lru_cache(maxsize=64)
def page_png(page: int, scale: float = 1.3) -> bytes:
    """PNG bytes of a 1-based page number."""
    with _lock:
        doc = _document()
        if not 1 <= page <= len(doc):
            raise ValueError(f"page {page} is outside 1..{len(doc)}")
        image = doc[page - 1].render(scale=scale).to_pil()
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
