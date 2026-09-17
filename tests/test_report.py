"""Tests for the Markdown report renderer in main.py."""
from __future__ import annotations

from main import _render_markdown, _report_filename, _slugify
from tools.extractor import ExtractedRule, SourceLocation, TechnicalRef
from agent.orchestrator import ContextAnswer


def _answer(**overrides) -> ContextAnswer:
    defaults = dict(
        component="services/payments",
        origin_description="branch feature/x -> main, local diff",
        coverage="partial",
        coverage_reason="Analise restrita ao diff.",
        rules=[
            ExtractedRule(
                rule_id="RN-001",
                description="Limite diario de R$ 8.000,00",
                condition="valor > 8000",
                category="business",
                confidence="high",
                origin="explicit_tag",
                status="code_confirmed",
                source_files=[SourceLocation(file="PaymentService.java", lines="8-10")],
                evidence="if (valor > 8000.0) { throw ... }",
            )
        ],
        technical_refs=[
            TechnicalRef(
                type="endpoint",
                name="POST /authorize",
                description="",
                source_files=[SourceLocation(file="PaymentController.java", lines="")],
            )
        ],
        summary="Servico de pagamentos com limite diario.",
    )
    defaults.update(overrides)
    return ContextAnswer(**defaults)


def test_slugify_handles_dot_and_special_chars() -> None:
    assert _slugify(".") == "root"
    assert _slugify("src/main/java/com/cielo/payments") == "src-main-java-com-cielo-payments"
    assert _slugify("") == "root"


def test_report_filename_includes_slug_and_timestamp() -> None:
    name = _report_filename("services/payments", generated_at="20260101-120000")
    assert name == "services-payments-20260101-120000.md"


def test_render_markdown_includes_all_sections() -> None:
    md = _render_markdown(_answer(), generated_at="20260101-120000")

    assert "# Contexto de Regras de Negócio — services/payments" in md
    assert "*Gerado em 20260101-120000*" in md
    assert "**Cobertura:** partial" in md
    assert "## Resumo" in md
    assert "Servico de pagamentos com limite diario." in md
    assert "## Regras (1)" in md
    assert "### [RN-001] Limite diario de R$ 8.000,00" in md
    assert "**Status:** code_confirmed" in md
    assert "`PaymentService.java:8-10`" in md
    assert "## Referências Técnicas (1)" in md
    assert "| endpoint | POST /authorize | `PaymentController.java` |" in md


def test_render_markdown_empty_rules_and_refs() -> None:
    md = _render_markdown(
        _answer(rules=[], technical_refs=[], summary=""),
        generated_at="20260101-120000",
    )
    assert "_Nenhuma regra encontrada._" in md
    assert "_Nenhuma referência técnica encontrada._" in md
    assert "(sem resumo)" in md
