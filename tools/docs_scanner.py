"""Scan documentation already committed in a repo: README, /docs/**/*.md,
ADRs, and Javadoc comments. Pure reads from disk, no LLM, no network — this is
Camada 0, the cheapest source of context before falling back to source code.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger("knowledge_agent.docs_scanner")

DocType = Literal["readme", "markdown_doc", "adr", "javadoc"]

_SKIP_DIRS = {".git", "target", "build", "node_modules", "dist", "out", ".venv", "venv"}
_MAX_CONTENT_CHARS = 4000
_MAX_JAVA_FILES_SCANNED = 200

_HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_JAVADOC_PATTERN = re.compile(
    r"/\*\*(?P<body>.*?)\*/\s*"
    # skip annotations / line comments between javadoc and declaration
    r"(?:@\w+(?:\([^)]*\))?\s*|//[^\n]*\s*)*"
    r"(?P<signature>(?:public|private|protected)[^\n{;]*)",
    re.DOTALL,
)
_JAVA_SYMBOL_NAME_PATTERN = re.compile(
    r"\b(?:class|interface)\s+(\w+)|(\w+)\s*\("
)


@dataclass(frozen=True)
class DocHit:
    path: str
    doc_type: DocType
    heading: str
    content: str


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_CONTENT_CHARS:
        return text
    return text[:_MAX_CONTENT_CHARS] + "\n[... truncado ...]"


def _first_heading(text: str, fallback: str) -> str:
    match = _HEADING_PATTERN.search(text)
    return match.group(1).strip() if match else fallback


def _iter_files(root: Path, *, suffix: str) -> list[Path]:
    if not root.exists():
        return []
    found: list[Path] = []
    for path in root.rglob(f"*{suffix}"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def _scan_readmes(repo_root: Path, scope_dir: Path) -> list[DocHit]:
    hits: list[DocHit] = []
    for candidate_dir in {repo_root, scope_dir}:
        for path in candidate_dir.glob("README*.md"):
            content = path.read_text(encoding="utf-8", errors="replace")
            hits.append(
                DocHit(
                    path=str(path.relative_to(repo_root)),
                    doc_type="readme",
                    heading=_first_heading(content, path.name),
                    content=_truncate(content),
                )
            )
    return hits


def _scan_markdown_docs(repo_root: Path) -> list[DocHit]:
    hits: list[DocHit] = []
    for docs_dir_name in ("docs", "adr"):
        for path in _iter_files(repo_root / docs_dir_name, suffix=".md"):
            content = path.read_text(encoding="utf-8", errors="replace")
            is_adr = "adr" in {p.lower() for p in path.parts}
            hits.append(
                DocHit(
                    path=str(path.relative_to(repo_root)),
                    doc_type="adr" if is_adr else "markdown_doc",
                    heading=_first_heading(content, path.name),
                    content=_truncate(content),
                )
            )
    return hits


def _extract_symbol_name(signature: str) -> Optional[str]:
    match = _JAVA_SYMBOL_NAME_PATTERN.search(signature)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _scan_javadoc(repo_root: Path, scope_dir: Path) -> list[DocHit]:
    hits: list[DocHit] = []
    java_files = _iter_files(scope_dir, suffix=".java")[:_MAX_JAVA_FILES_SCANNED]
    for path in java_files:
        content = path.read_text(encoding="utf-8", errors="replace")
        for match in _JAVADOC_PATTERN.finditer(content):
            body = match.group("body")
            cleaned = "\n".join(
                line.strip().lstrip("*").strip() for line in body.splitlines()
            ).strip()
            if not cleaned:
                continue
            symbol = _extract_symbol_name(match.group("signature"))
            hits.append(
                DocHit(
                    path=str(path.relative_to(repo_root)),
                    doc_type="javadoc",
                    heading=symbol or path.stem,
                    content=_truncate(cleaned),
                )
            )
    return hits


def find_repo_docs(repo_root: Path, *, area: Optional[str] = None) -> list[DocHit]:
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Repo root not found: {repo_root}")

    scope_dir = (repo_root / area) if area else repo_root
    if not scope_dir.is_dir():
        raise FileNotFoundError(f"Area not found inside repo: {scope_dir}")

    logger.info("Camada 0: escaneando docs em %s (area=%s)", repo_root, area or "*")
    start = time.monotonic()
    hits: list[DocHit] = []
    hits.extend(_scan_readmes(repo_root, scope_dir))
    hits.extend(_scan_markdown_docs(repo_root))
    hits.extend(_scan_javadoc(repo_root, scope_dir))
    logger.info(
        "Camada 0 concluida em %.2fs: %d doc(s) encontrado(s)",
        time.monotonic() - start,
        len(hits),
    )
    return hits
