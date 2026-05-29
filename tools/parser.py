"""Parse PDF and Word documents into raw text."""
from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}


class UnsupportedDocumentError(Exception):
    """Raised when a file extension is not supported by the parser."""


def parse(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Document not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(p)
    if suffix in (".docx", ".doc"):
        return _parse_docx(p)
    raise UnsupportedDocumentError(
        f"Unsupported extension '{suffix}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}"
    )


def _parse_pdf(path: Path) -> str:
    import pymupdf  # type: ignore

    chunks: list[str] = []
    with pymupdf.open(path) as doc:
        for page in doc:
            chunks.append(page.get_text("text"))
    text = "\n".join(chunks).strip()
    if not text:
        raise ValueError(f"PDF produced no extractable text: {path}")
    return text


def _parse_docx(path: Path) -> str:
    from docx import Document  # type: ignore

    doc = Document(str(path))
    parts: list[str] = []
    for paragraph in doc.paragraphs:
        if paragraph.text:
            parts.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError(f"Word document produced no extractable text: {path}")
    return text
