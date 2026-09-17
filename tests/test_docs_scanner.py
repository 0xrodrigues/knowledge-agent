"""Tests for tools/docs_scanner.py (Camada 0) against the tmp_java_repo fixture."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import docs_scanner


def test_finds_readme(tmp_java_repo: Path) -> None:
    hits = docs_scanner.find_repo_docs(tmp_java_repo)
    readme_hits = [h for h in hits if h.doc_type == "readme"]
    assert len(readme_hits) == 1
    assert "RN-001" in readme_hits[0].content
    assert "5.000" in readme_hits[0].content


def test_finds_adr(tmp_java_repo: Path) -> None:
    hits = docs_scanner.find_repo_docs(tmp_java_repo)
    adr_hits = [h for h in hits if h.doc_type == "adr"]
    assert len(adr_hits) == 1
    assert "limite" in adr_hits[0].content.lower()


def test_finds_javadoc_comments(tmp_java_repo: Path) -> None:
    hits = docs_scanner.find_repo_docs(tmp_java_repo)
    javadoc_hits = [h for h in hits if h.doc_type == "javadoc"]
    assert len(javadoc_hits) >= 1
    assert any("RN-001" in h.content for h in javadoc_hits)


def test_area_scopes_javadoc_but_not_top_level_docs(tmp_java_repo: Path) -> None:
    hits = docs_scanner.find_repo_docs(
        tmp_java_repo, area="src/main/java/com/cielo/payments"
    )
    assert any(h.doc_type == "readme" for h in hits)
    assert any(h.doc_type == "javadoc" for h in hits)


def test_missing_area_raises(tmp_java_repo: Path) -> None:
    with pytest.raises(FileNotFoundError):
        docs_scanner.find_repo_docs(tmp_java_repo, area="does/not/exist")


def test_missing_repo_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        docs_scanner.find_repo_docs(tmp_path / "nope")
