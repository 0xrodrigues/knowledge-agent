"""End-to-end CLI tests, in-process (main.main(argv)) — no subprocess, no
network. The graph is redirected to a tmp_path DB and the LLM-calling
extractor functions are monkeypatched, so this never costs a token nor
touches the project's real data/graph.db.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import main
from graph.knowledge_graph import KnowledgeGraph
from tools.extractor import CodeContext, ExtractedRule, SourceLocation

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "graph" / "schema.sql"
SERVICE_PATH = "src/main/java/com/cielo/payments/PaymentService.java"


@pytest.fixture(autouse=True)
def isolated_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "graph.db"

    def factory(*args, **kwargs):
        return KnowledgeGraph(db_path=db_path, schema_path=SCHEMA_PATH)

    monkeypatch.setattr("agent.orchestrator.KnowledgeGraph", factory)
    monkeypatch.setattr("main.KnowledgeGraph", factory)


@pytest.fixture(autouse=True)
def fake_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_docs(component, hits):
        return CodeContext(component=component, rules=[], technical_refs=[], summary="")

    def fake_code(*, component, files, mode, known_rules, doc_candidates):
        return CodeContext(
            component=component,
            rules=[
                ExtractedRule(
                    rule_id="RN-001",
                    description="Limite diario de R$ 8.000,00",
                    condition="valor > 8000",
                    category="business",
                    confidence="high",
                    origin="explicit_tag",
                    status="code_confirmed",
                    source_files=[SourceLocation(file=SERVICE_PATH, lines="8-10")],
                    evidence="if (valor > 8000.0) { throw ... }",
                )
            ],
            technical_refs=[],
            summary="Servico de pagamentos com limite diario.",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_code)


def test_context_branch_json_output(
    capsys: pytest.CaptureFixture, tmp_java_repo: Path
) -> None:
    rc = main.main(
        [
            "context",
            "--branch",
            "feature/raise-daily-limit",
            "--base",
            "main",
            "--repo-path",
            str(tmp_java_repo),
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["component"]
    rule_ids = {r["rule_id"] for r in payload["rules"]}
    assert "RN-001" in rule_ids
    assert payload["coverage"] == "partial"


def test_context_human_readable_output(
    capsys: pytest.CaptureFixture, tmp_java_repo: Path
) -> None:
    rc = main.main(
        [
            "context",
            "--branch",
            "feature/raise-daily-limit",
            "--base",
            "main",
            "--repo-path",
            str(tmp_java_repo),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cobertura" in out
    assert "RN-001" in out


def test_context_missing_origin_args_errors(capsys: pytest.CaptureFixture) -> None:
    rc = main.main(["context", "--pr", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Erro" in err


def test_status_and_list_after_context(
    capsys: pytest.CaptureFixture, tmp_java_repo: Path
) -> None:
    main.main(
        [
            "context",
            "--branch",
            "feature/raise-daily-limit",
            "--base",
            "main",
            "--repo-path",
            str(tmp_java_repo),
            "--json",
        ]
    )
    capsys.readouterr()  # discard context output

    rc = main.main(["status"])
    assert rc == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["components"] >= 1
    assert stats["rules"] >= 1

    rc = main.main(["list"])
    assert rc == 0
    listing = capsys.readouterr().out
    assert "regra" in listing
