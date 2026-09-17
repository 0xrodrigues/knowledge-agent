"""Tests for tools/extractor.py — tools.openrouter.chat is always monkeypatched,
never calls the real OpenRouter API."""
from __future__ import annotations

import json

import pytest

from tools import extractor
from tools.docs_scanner import DocHit
from tools.openrouter import OpenRouterError


def _fail_if_called(*args, **kwargs):
    raise AssertionError("chat() should not have been called")


# ------------------------------------------------------------------ rule_id


@pytest.mark.parametrize("value", ["RN-001", "RN-1", "RN-AUTO-1", "RN-AUTO-42"])
def test_extracted_rule_accepts_valid_rule_ids(value: str) -> None:
    rule = extractor.ExtractedRule(
        rule_id=value,
        description="x",
        category="business",
        confidence="high",
        origin="inferred",
        status="code_only",
    )
    assert rule.rule_id == value


def test_extracted_rule_rejects_garbage_rule_id() -> None:
    with pytest.raises(Exception, match="must look like"):
        extractor.ExtractedRule(
            rule_id="not-a-rule-id",
            description="x",
            category="business",
            confidence="high",
            origin="inferred",
            status="code_only",
        )


# ------------------------------------------------------------ extract_from_docs


def test_extract_from_docs_no_hits_skips_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "chat", _fail_if_called)
    result = extractor.extract_from_docs("services/payments", [])
    assert result.rules == []
    assert result.technical_refs == []


def test_extract_from_docs_forces_repo_doc_status(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "component": "services/payments",
        "rules": [
            {
                "rule_id": "RN-001",
                "description": "Limite diario de R$ 5.000,00",
                "condition": "valor > 5000",
                "category": "business",
                "confidence": "high",
                # Deliberately wrong values the LLM might hallucinate —
                # extract_from_docs must override these regardless.
                "origin": "explicit_tag",
                "status": "code_confirmed",
                "source_files": [{"file": "README.md", "lines": ""}],
                "evidence": "RN-001: limite diario de R$ 5.000,00",
            }
        ],
        "technical_refs": [],
        "summary": "Documento descreve limite diario.",
    }
    monkeypatch.setattr(extractor, "chat", lambda **kwargs: json.dumps(payload))

    hits = [DocHit(path="README.md", doc_type="readme", heading="Payments", content="RN-001...")]
    result = extractor.extract_from_docs("services/payments", hits)

    assert len(result.rules) == 1
    assert result.rules[0].origin == "repo_doc"
    assert result.rules[0].status == "doc_only"
    assert result.rules[0].rule_id == "RN-001"


def test_extract_from_docs_repairs_unescaped_backslash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a real run had the LLM copy 'R\\$ 5.000,00' from a README
    straight into a JSON string without doubling the backslash — strict
    json.loads rejects that ('Invalid \\escape'). The repair pass must fix
    it instead of failing the whole extraction."""
    raw = (
        '{"component": "x", "rules": [{"rule_id": null, '
        '"description": "Frete gratis acima de R\\$ 200,00", "condition": "", '
        '"category": "business", "confidence": "high", "origin": "repo_doc", '
        '"status": "doc_only", "source_files": [], '
        '"evidence": "gratis para pedidos acima de R\\$ 200,00"}], '
        '"technical_refs": [], "summary": ""}'
    )
    monkeypatch.setattr(extractor, "chat", lambda **kwargs: raw)
    hits = [DocHit(path="README.md", doc_type="readme", heading="x", content="y")]

    result = extractor.extract_from_docs("x", hits)

    assert len(result.rules) == 1
    assert "R\\$ 200,00" in result.rules[0].description


def test_extract_from_docs_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "chat", lambda **kwargs: "not json at all")
    hits = [DocHit(path="README.md", doc_type="readme", heading="x", content="y")]
    with pytest.raises(extractor.ExtractionError, match="invalid JSON"):
        extractor.extract_from_docs("services/payments", hits)


def test_extract_from_docs_schema_violation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        extractor, "chat", lambda **kwargs: json.dumps({"component": "x"})
    )
    hits = [DocHit(path="README.md", doc_type="readme", heading="x", content="y")]
    # missing required top-level structure is fine (defaults), but a bad rule shape isn't
    monkeypatch.setattr(
        extractor,
        "chat",
        lambda **kwargs: json.dumps(
            {"component": "x", "rules": [{"description": "no category field"}]}
        ),
    )
    with pytest.raises(extractor.ExtractionError, match="schema validation"):
        extractor.extract_from_docs("services/payments", hits)


def test_extract_from_docs_llm_call_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs):
        raise OpenRouterError("HTTP 500")

    monkeypatch.setattr(extractor, "chat", boom)
    hits = [DocHit(path="README.md", doc_type="readme", heading="x", content="y")]
    with pytest.raises(extractor.ExtractionError, match="LLM call failed"):
        extractor.extract_from_docs("services/payments", hits)


# ------------------------------------------------------------ extract_from_code


def test_extract_from_code_no_files_skips_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "chat", _fail_if_called)
    result = extractor.extract_from_code(
        component="services/payments",
        files={},
        mode="pr_diff",
        known_rules=[],
        doc_candidates=[],
    )
    assert result.rules == []


def test_extract_from_code_parses_contradiction(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "component": "services/payments",
        "rules": [
            {
                "rule_id": "RN-001",
                "description": "Limite diario de R$ 8.000,00",
                "condition": "valor > 8000",
                "category": "business",
                "confidence": "high",
                "origin": "explicit_tag",
                "status": "code_contradicts_doc",
                "source_files": [{"file": "PaymentService.java", "lines": "8-10"}],
                "evidence": "if (valor > 8000.0) { throw ... }",
            }
        ],
        "technical_refs": [
            {
                "type": "endpoint",
                "name": "POST /payments/authorize",
                "description": "Autoriza pagamento",
                "source_files": [],
            }
        ],
        "summary": "Servico autoriza pagamentos ate 8000.",
    }
    monkeypatch.setattr(extractor, "chat", lambda **kwargs: json.dumps(payload))

    result = extractor.extract_from_code(
        component="services/payments",
        files={"PaymentService.java": "public class PaymentService { ... }"},
        mode="pr_diff",
        known_rules=[],
        doc_candidates=[],
    )

    assert result.rules[0].status == "code_contradicts_doc"
    assert result.technical_refs[0].type == "endpoint"


def test_extract_from_code_drops_unrecognized_ref_type_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a real run against a JS/HTML repo had the LLM label a
    technical_ref with a type outside our closed enum. That used to blow up
    the whole batch (losing valid rules along with it) — it must now just
    drop that one ref and keep going."""
    payload = {
        "component": "frontend",
        "rules": [
            {
                "rule_id": None,
                "description": "Desconto de 10% para clientes recorrentes",
                "condition": "previousOrders > 5",
                "category": "business",
                "confidence": "high",
                "origin": "inferred",
                "status": "code_only",
                "source_files": [{"file": "cart.js", "lines": "12-16"}],
                "evidence": "if (previousOrders > 5) { return subtotal * 0.9; }",
            }
        ],
        "technical_refs": [
            {"type": "widget", "name": "cart.js", "description": "", "source_files": []},
            {"type": "endpoint", "name": "POST /checkout", "description": "", "source_files": []},
        ],
        "summary": "",
    }
    monkeypatch.setattr(extractor, "chat", lambda **kwargs: json.dumps(payload))

    result = extractor.extract_from_code(
        component="frontend",
        files={"cart.js": "..."},
        mode="pr_diff",
        known_rules=[],
        doc_candidates=[],
    )

    assert len(result.rules) == 1
    assert len(result.technical_refs) == 1
    assert result.technical_refs[0].name == "POST /checkout"


def test_extract_from_code_llm_call_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs):
        raise OpenRouterError("timeout")

    monkeypatch.setattr(extractor, "chat", boom)
    with pytest.raises(extractor.ExtractionError, match="LLM call failed"):
        extractor.extract_from_code(
            component="services/payments",
            files={"a.java": "class A {}"},
            mode="pr_diff",
            known_rules=[],
            doc_candidates=[],
        )
