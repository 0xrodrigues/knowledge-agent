"""Tests for tools/code_scanner.py against real Java source (tmp_java_repo)."""
from __future__ import annotations

from pathlib import Path

from tools import code_scanner

CONTROLLER_PATH = "src/main/java/com/cielo/payments/PaymentController.java"
SERVICE_PATH = "src/main/java/com/cielo/payments/PaymentService.java"
ENTITY_PATH = "src/main/java/com/cielo/payments/Payment.java"


def _read(tmp_java_repo: Path, rel_path: str) -> str:
    return (tmp_java_repo / rel_path).read_text(encoding="utf-8")


def test_extract_symbols_finds_class_and_methods(tmp_java_repo: Path) -> None:
    content = _read(tmp_java_repo, CONTROLLER_PATH)
    symbols = code_scanner.extract_symbols(CONTROLLER_PATH, content)

    kinds_names = {(s.kind, s.name) for s in symbols}
    assert ("class", "PaymentController") in kinds_names
    assert ("method", "authorize") in kinds_names


def test_extract_symbols_captures_annotations(tmp_java_repo: Path) -> None:
    content = _read(tmp_java_repo, CONTROLLER_PATH)
    symbols = code_scanner.extract_symbols(CONTROLLER_PATH, content)

    controller_class = next(s for s in symbols if s.name == "PaymentController" and s.kind == "class")
    assert any(a.startswith("@RestController") for a in controller_class.annotations)

    authorize_method = next(s for s in symbols if s.name == "authorize" and s.kind == "method")
    assert any(a.startswith("@PostMapping") for a in authorize_method.annotations)


def test_extract_symbols_service_annotation(tmp_java_repo: Path) -> None:
    content = _read(tmp_java_repo, SERVICE_PATH)
    symbols = code_scanner.extract_symbols(SERVICE_PATH, content)
    service_class = next(s for s in symbols if s.name == "PaymentService" and s.kind == "class")
    assert any(a.startswith("@Service") for a in service_class.annotations)


def test_infer_technical_refs_endpoint(tmp_java_repo: Path) -> None:
    content = _read(tmp_java_repo, CONTROLLER_PATH)
    symbols = code_scanner.extract_symbols(CONTROLLER_PATH, content)
    refs = code_scanner.infer_technical_refs(symbols)

    endpoint_refs = [r for r in refs if r.type == "endpoint"]
    assert len(endpoint_refs) == 1
    assert endpoint_refs[0].name == "POST /authorize"


def test_infer_technical_refs_table(tmp_java_repo: Path) -> None:
    content = _read(tmp_java_repo, ENTITY_PATH)
    symbols = code_scanner.extract_symbols(ENTITY_PATH, content)
    refs = code_scanner.infer_technical_refs(symbols)

    table_refs = [r for r in refs if r.type == "table"]
    assert len(table_refs) == 1
    assert table_refs[0].name == "payments"


def test_infer_technical_refs_bean_class(tmp_java_repo: Path) -> None:
    content = _read(tmp_java_repo, SERVICE_PATH)
    symbols = code_scanner.extract_symbols(SERVICE_PATH, content)
    refs = code_scanner.infer_technical_refs(symbols)

    class_refs = [r for r in refs if r.type == "class"]
    assert len(class_refs) == 1
    assert class_refs[0].name == "PaymentService"


def test_expand_call_chain_follows_usage(tmp_java_repo: Path) -> None:
    scope = code_scanner.expand_call_chain(
        tmp_java_repo, [SERVICE_PATH], max_depth=2, max_files=10
    )
    assert CONTROLLER_PATH in scope.related_files
    assert scope.truncated is False


def test_expand_call_chain_respects_max_files(tmp_java_repo: Path) -> None:
    scope = code_scanner.expand_call_chain(
        tmp_java_repo, [SERVICE_PATH], max_depth=2, max_files=1
    )
    assert len(scope.related_files) <= 1
    assert scope.truncated is True


def test_chunk_for_llm_splits_by_size() -> None:
    files = {"A.java": "x" * 50, "B.java": "y" * 50, "C.java": "z" * 50}
    batches = code_scanner.chunk_for_llm(files, max_chars=70)
    assert len(batches) >= 2
    for batch in batches:
        total = sum(len(k) + len(v) for k, v in batch.items())
        assert total <= 70 or len(batch) == 1  # single oversized file allowed alone


def test_chunk_for_llm_truncates_oversized_single_file() -> None:
    files = {"Huge.java": "x" * 200}
    batches = code_scanner.chunk_for_llm(files, max_chars=50)
    assert len(batches) == 1
    content = batches[0]["Huge.java"]
    assert content.endswith("[... truncado ...]")
    assert len(content) < 200
