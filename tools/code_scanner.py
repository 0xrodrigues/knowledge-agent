"""Java/Spring-Boot-aware code scanning: Camada 2 helpers.

Symbol extraction is heuristic (regex over source text, not a real parser) —
deliberately scoped to the idiomatic Spring Boot patterns (annotations right
above class/method declarations). Good enough to drive call-chain expansion
and to infer high-confidence technical_refs directly from annotations,
without spending an LLM call on facts the annotation already states.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools import vcs
from tools.extractor import SourceLocation, TechnicalRef

logger = logging.getLogger("knowledge_agent.code_scanner")

JavaSymbolKind = Literal["class", "interface", "method"]

_ANNOTATION_LINE_PATTERN = re.compile(r"^\s*@(\w+)")
_TYPE_DECL_PATTERN = re.compile(
    r"^\s*(?:public|private|protected)?\s*"
    r"(?:abstract\s+|final\s+|static\s+)*"
    r"\b(class|interface)\s+(\w+)"
)
_METHOD_DECL_PATTERN = re.compile(
    r"^\s*(?:public|private|protected)\s+"
    r"(?:static\s+|final\s+|synchronized\s+|abstract\s+)*"
    r"[\w<>\[\],.\s]*?\s*(\w+)\s*\([^;{]*\)\s*"
    r"(?:throws\s+[\w,\s]+)?\s*\{"
)
_COMMENT_PREFIXES = ("//", "*", "/*")

_ENDPOINT_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "REQUEST",
}
_MAPPING_VALUE_PATTERN = re.compile(r"\(\s*(?:value\s*=\s*)?[\"']([^\"']*)[\"']")
_TABLE_NAME_PATTERN = re.compile(r"name\s*=\s*[\"']([^\"']+)[\"']")
_BEAN_ANNOTATIONS = {"Service", "Component", "Repository"}


@dataclass(frozen=True)
class JavaSymbol:
    kind: JavaSymbolKind
    name: str
    annotations: list[str]
    file: str
    line_range: str


@dataclass(frozen=True)
class ExpandedScope:
    seed_files: list[str]
    related_files: list[str]
    truncated: bool
    depth_used: int


def _find_block_end(lines: list[str], start_idx: int) -> int:
    depth = 0
    started = False
    for i in range(start_idx, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        if started and depth <= 0:
            return i
    return len(lines) - 1


def extract_symbols(file_path: str, content: str) -> list[JavaSymbol]:
    lines = content.splitlines()
    symbols: list[JavaSymbol] = []
    pending_annotations: list[str] = []

    for idx, line in enumerate(lines):
        if _ANNOTATION_LINE_PATTERN.match(line):
            pending_annotations.append(line.strip())
            continue

        type_match = _TYPE_DECL_PATTERN.match(line)
        if type_match:
            end_line = _find_block_end(lines, idx)
            symbols.append(
                JavaSymbol(
                    kind=type_match.group(1),  # type: ignore[arg-type]
                    name=type_match.group(2),
                    annotations=list(pending_annotations),
                    file=file_path,
                    line_range=f"{idx + 1}-{end_line + 1}",
                )
            )
            pending_annotations = []
            continue

        method_match = _METHOD_DECL_PATTERN.match(line)
        if method_match:
            end_line = _find_block_end(lines, idx)
            symbols.append(
                JavaSymbol(
                    kind="method",
                    name=method_match.group(1),
                    annotations=list(pending_annotations),
                    file=file_path,
                    line_range=f"{idx + 1}-{end_line + 1}",
                )
            )
            pending_annotations = []
            continue

        stripped = line.strip()
        if stripped and not stripped.startswith(_COMMENT_PREFIXES):
            pending_annotations = []

    return symbols


def infer_technical_refs(symbols: list[JavaSymbol]) -> list[TechnicalRef]:
    refs: list[TechnicalRef] = []
    for symbol in symbols:
        for annotation in symbol.annotations:
            ann_match = re.match(r"@(\w+)", annotation)
            if not ann_match:
                continue
            ann_name = ann_match.group(1)

            if ann_name in _ENDPOINT_ANNOTATIONS and symbol.kind == "method":
                path_match = _MAPPING_VALUE_PATTERN.search(annotation)
                path = path_match.group(1) if path_match else ""
                method = _ENDPOINT_ANNOTATIONS[ann_name]
                refs.append(
                    TechnicalRef(
                        type="endpoint",
                        name=f"{method} {path}".strip(),
                        description=f"{symbol.name} ({symbol.file})",
                        source_files=[
                            SourceLocation(file=symbol.file, lines=symbol.line_range)
                        ],
                    )
                )

            if ann_name == "Table" and symbol.kind in ("class", "interface"):
                name_match = _TABLE_NAME_PATTERN.search(annotation)
                table_name = name_match.group(1) if name_match else symbol.name
                refs.append(
                    TechnicalRef(
                        type="table",
                        name=table_name,
                        description=symbol.name,
                        source_files=[
                            SourceLocation(file=symbol.file, lines=symbol.line_range)
                        ],
                    )
                )

            if ann_name in _BEAN_ANNOTATIONS and symbol.kind in ("class", "interface"):
                refs.append(
                    TechnicalRef(
                        type="class",
                        name=symbol.name,
                        description=f"Spring bean ({ann_name})",
                        source_files=[
                            SourceLocation(file=symbol.file, lines=symbol.line_range)
                        ],
                    )
                )
    return refs


def expand_call_chain(
    repo_root: Path,
    seed_files: list[str],
    *,
    max_depth: int = 2,
    max_files: int = 40,
) -> ExpandedScope:
    repo_root = Path(repo_root)
    visited: set[str] = set(seed_files)
    frontier: set[str] = set(seed_files)
    truncated = False
    depth_used = 0
    start = time.monotonic()
    logger.info(
        "Camada 2: expandindo cadeia de chamadas a partir de %d arquivo(s) semente "
        "(max_depth=%d, max_files=%d)",
        len(seed_files),
        max_depth,
        max_files,
    )

    for depth in range(1, max_depth + 1):
        depth_used = depth
        next_frontier: set[str] = set()

        for file_path in frontier:
            abs_path = repo_root / file_path
            if not abs_path.is_file():
                continue
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            for symbol in extract_symbols(file_path, content):
                if symbol.kind != "method":
                    continue
                hits = vcs.grep_symbol(repo_root, f"{symbol.name}(", max_results=50)
                for hit in hits:
                    if hit.file in visited:
                        continue
                    if len(visited) >= max_files:
                        truncated = True
                        break
                    visited.add(hit.file)
                    next_frontier.add(hit.file)
                if len(visited) >= max_files:
                    truncated = True
                    break
            if len(visited) >= max_files:
                break

        logger.debug(
            "Profundidade %d: %d arquivo(s) visitado(s) ate agora", depth, len(visited)
        )
        if len(visited) >= max_files:
            break
        if not next_frontier:
            break
        frontier = next_frontier

    related_files = sorted(visited - set(seed_files))
    logger.info(
        "Cadeia de chamadas expandida em %.2fs: %d arquivo(s) relacionado(s) "
        "encontrado(s) ate profundidade %d%s",
        time.monotonic() - start,
        len(related_files),
        depth_used,
        " (truncado)" if truncated else "",
    )
    return ExpandedScope(
        seed_files=list(seed_files),
        related_files=related_files,
        truncated=truncated,
        depth_used=depth_used,
    )


def chunk_for_llm(files: dict[str, str], *, max_chars: int) -> list[dict[str, str]]:
    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_size = 0

    for path, content in files.items():
        entry_size = len(path) + len(content)

        if entry_size > max_chars:
            if current:
                batches.append(current)
                current = {}
                current_size = 0
            batches.append({path: content[:max_chars] + "\n[... truncado ...]"})
            continue

        if current and current_size + entry_size > max_chars:
            batches.append(current)
            current = {}
            current_size = 0

        current[path] = content
        current_size += entry_size

    if current:
        batches.append(current)
    return batches
