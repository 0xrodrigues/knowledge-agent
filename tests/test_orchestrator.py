"""Tests for agent/orchestrator.py.

vcs/docs_scanner/code_scanner run for real against tmp_java_repo (no network,
no LLM). Only tools.extractor.extract_from_docs/extract_from_code — the only
functions that would call the real OpenRouter API — are monkeypatched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.orchestrator import ContextRequest, Orchestrator, OrchestratorError
from graph.knowledge_graph import KnowledgeGraph
from tools.extractor import CodeContext, ExtractedRule, ExtractionError, SourceLocation, TechnicalRef

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "graph" / "schema.sql"
SERVICE_PATH = "src/main/java/com/cielo/payments/PaymentService.java"
CONTROLLER_PATH = "src/main/java/com/cielo/payments/PaymentController.java"


@pytest.fixture()
def orchestrator(tmp_path: Path) -> Orchestrator:
    graph = KnowledgeGraph(db_path=tmp_path / "graph.db", schema_path=SCHEMA_PATH)
    return Orchestrator(graph=graph)


def _empty_context(component: str) -> CodeContext:
    return CodeContext(component=component, rules=[], technical_refs=[], summary="")


def _code_confirmed_rule(status: str = "code_confirmed", description: str = "Limite diario de R$ 8.000,00") -> ExtractedRule:
    return ExtractedRule(
        rule_id="RN-001",
        description=description,
        condition="valor > 8000",
        category="business",
        confidence="high",
        origin="explicit_tag",
        status=status,  # type: ignore[arg-type]
        source_files=[SourceLocation(file=SERVICE_PATH, lines="8-10")],
        evidence="if (valor > 8000.0) { throw ... }",
    )


# --------------------------------------------------------------- validation


def test_pr_number_without_repo_path_or_repo_raises(orchestrator: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="repo_path.*or.*repo|repo.*repo_path"):
        orchestrator.answer(ContextRequest(mode="pr_number", pr_number=1))


def test_branch_without_repo_path_raises(orchestrator: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="repo_path"):
        orchestrator.answer(ContextRequest(mode="branch", branch="feature/x"))


def test_area_without_repo_path_raises(orchestrator: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="repo_path"):
        orchestrator.answer(ContextRequest(mode="area", area="services"))


def test_pr_number_without_repo_path_or_repo_still_required(orchestrator: Orchestrator) -> None:
    with pytest.raises(OrchestratorError, match="repo_path"):
        orchestrator.answer(ContextRequest(mode="pr_number", pr_number=1))


def test_branch_mode_auto_clones_when_only_repo_given(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    """No repo_path, only `repo` — the agent clones it itself instead of the
    caller having to run `git clone` by hand. clone_or_update is faked to
    just point at the already-built tmp_java_repo fixture, so this never
    touches the real network."""

    def fake_clone_or_update(url, dest):
        assert url == "https://github.com/example-org/payments.git"
        return tmp_java_repo

    monkeypatch.setattr("agent.orchestrator.vcs.clone_or_update", fake_clone_or_update)
    monkeypatch.setattr("agent.orchestrator.extract_from_docs", lambda component, hits: _empty_context(component))
    monkeypatch.setattr(
        "agent.orchestrator.extract_from_code",
        lambda **kwargs: CodeContext(component=kwargs["component"], rules=[], technical_refs=[], summary=""),
    )

    request = ContextRequest(
        mode="branch",
        branch="feature/raise-daily-limit",
        base_branch="main",
        repo="example-org/payments",
    )
    answer = orchestrator.answer(request)
    assert answer.component  # resolved and ran without needing a manual clone


# --------------------------------------------------------------- branch mode


def test_branch_mode_pr_diff_persists_and_returns_rule(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    calls = {"code": 0}

    def fake_extract_from_docs(component, hits):
        return _empty_context(component)

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        calls["code"] += 1
        assert SERVICE_PATH in files
        return CodeContext(
            component=component,
            rules=[_code_confirmed_rule()],
            technical_refs=[],
            summary="Servico de pagamentos com limite diario.",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="branch",
        branch="feature/raise-daily-limit",
        base_branch="main",
        repo_path=tmp_java_repo,
    )
    answer = orchestrator.answer(request)

    assert calls["code"] == 1
    assert answer.coverage == "partial"
    rule_ids = {r.rule_id for r in answer.rules}
    assert "RN-001" in rule_ids

    persisted = orchestrator.graph.get_rule("RN-001")
    assert persisted is not None
    assert persisted.status == "code_confirmed"


def test_camada1_skips_llm_call_on_second_identical_run(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    calls = {"code": 0}

    def fake_extract_from_docs(component, hits):
        return _empty_context(component)

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        calls["code"] += 1
        return CodeContext(
            component=component,
            rules=[_code_confirmed_rule()],
            technical_refs=[],
            summary="",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="branch",
        branch="feature/raise-daily-limit",
        base_branch="main",
        repo_path=tmp_java_repo,
    )
    orchestrator.answer(request)
    assert calls["code"] == 1

    second_answer = orchestrator.answer(request)
    assert calls["code"] == 1  # not called again — content hash matched the cache
    rule_ids = {r.rule_id for r in second_answer.rules}
    assert "RN-001" in rule_ids


def test_inferred_rule_gets_resolved_rn_auto_id_in_answer(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    """Regression: a real run had every rule without an explicit RN-XX tag
    print as '[???]' — _persist allocated RN-AUTO-<n> in the DB but never
    wrote it back into the ExtractedRule objects returned to the caller."""

    def fake_extract_from_docs(component, hits):
        return _empty_context(component)

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        return CodeContext(
            component=component,
            rules=[
                ExtractedRule(
                    rule_id=None,
                    description="Regra sem tag explicita no codigo",
                    condition="",
                    category="business",
                    confidence="medium",
                    origin="inferred",
                    status="code_only",
                    source_files=[SourceLocation(file=SERVICE_PATH, lines="1-5")],
                    evidence="...",
                )
            ],
            technical_refs=[],
            summary="",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="branch",
        branch="feature/raise-daily-limit",
        base_branch="main",
        repo_path=tmp_java_repo,
    )
    answer = orchestrator.answer(request)

    assert len(answer.rules) == 1
    assert answer.rules[0].rule_id is not None
    assert answer.rules[0].rule_id.startswith("RN-AUTO-")
    # and the graph itself is consistent with what was returned
    persisted = orchestrator.graph.get_rule(answer.rules[0].rule_id)
    assert persisted is not None


def test_code_contradicts_doc_wins_over_doc_candidate(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    def fake_extract_from_docs(component, hits):
        return CodeContext(
            component=component,
            rules=[
                ExtractedRule(
                    rule_id="RN-001",
                    description="Limite diario de R$ 5.000,00",
                    condition="valor > 5000",
                    category="business",
                    confidence="medium",
                    origin="repo_doc",
                    status="doc_only",
                    source_files=[SourceLocation(file="README.md", lines="")],
                    evidence="RN-001: ... R$ 5.000,00",
                )
            ],
            technical_refs=[],
            summary="",
        )

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        assert doc_candidates and doc_candidates[0].rule_id == "RN-001"
        return CodeContext(
            component=component,
            rules=[_code_confirmed_rule(status="code_contradicts_doc")],
            technical_refs=[],
            summary="",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="branch",
        branch="feature/raise-daily-limit",
        base_branch="main",
        repo_path=tmp_java_repo,
    )
    answer = orchestrator.answer(request)

    rn001 = next(r for r in answer.rules if r.rule_id == "RN-001")
    assert rn001.status == "code_contradicts_doc"
    assert "8.000" in rn001.description or "8000" in rn001.description


# ------------------------------------------------------------------ area mode


def test_area_mode_full_flow_marks_full_scan(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    def fake_extract_from_docs(component, hits):
        return _empty_context(component)

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        assert mode == "full_flow"
        return CodeContext(
            component=component,
            rules=[_code_confirmed_rule(description="Limite diario de R$ 5.000,00")],
            technical_refs=[
                TechnicalRef(type="endpoint", name="POST /authorize", description="", source_files=[])
            ],
            summary="",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="area",
        area="src/main/java/com/cielo/payments",
        repo_path=tmp_java_repo,
        full_flow=True,
    )
    answer = orchestrator.answer(request)

    assert answer.coverage == "full"
    component_record = orchestrator.graph.get_component("src/main/java/com/cielo/payments")
    assert component_record is not None
    assert component_record.last_full_scan_commit is not None


def test_parallel_batches_aggregate_all_results(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    """3 seed files, tiny max_chars forces 3 separate batches — run in
    parallel via ThreadPoolExecutor. Every batch's rules must still show up
    in the final answer, regardless of completion order."""
    monkeypatch.setattr("agent.orchestrator._MAX_CHARS_PER_BATCH", 1)

    calls: list[str] = []

    def fake_extract_from_docs(component, hits):
        return _empty_context(component)

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        (path,) = files.keys()
        calls.append(path)
        # Deterministic per-path numeric id (rule_id must match RN-\d{1,4}),
        # no shared counter — avoids a race between concurrent threads both
        # reading the same counter value.
        digits = abs(hash(path)) % 9000 + 100
        rule = _code_confirmed_rule(description=f"Regra de {path}").model_copy(
            update={"rule_id": f"RN-{digits}"}
        )
        return CodeContext(
            component=component,
            rules=[rule],
            technical_refs=[],
            summary="",
        )

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="area",
        area="src/main/java/com/cielo/payments",
        repo_path=tmp_java_repo,
        full_flow=True,
    )
    answer = orchestrator.answer(request)

    assert len(calls) == 3  # one batch per file — confirms it didn't collapse into one
    assert len(answer.rules) == 3


def test_parallel_batch_error_fails_whole_request(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    """One batch failing must still fail the whole request under the
    parallel executor, same as the old sequential loop did."""
    monkeypatch.setattr("agent.orchestrator._MAX_CHARS_PER_BATCH", 1)

    def fake_extract_from_docs(component, hits):
        return _empty_context(component)

    def fake_extract_from_code(*, component, files, mode, known_rules, doc_candidates):
        (path,) = files.keys()
        if path.endswith("Payment.java"):
            raise ExtractionError("simulated failure for this batch")
        return CodeContext(component=component, rules=[], technical_refs=[], summary="")

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", fake_extract_from_docs)
    monkeypatch.setattr("agent.orchestrator.extract_from_code", fake_extract_from_code)

    request = ContextRequest(
        mode="area",
        area="src/main/java/com/cielo/payments",
        repo_path=tmp_java_repo,
        full_flow=True,
    )
    with pytest.raises(OrchestratorError, match="simulated failure"):
        orchestrator.answer(request)


# --------------------------------------------------------------------- errors


def test_extraction_error_logs_failure_operation(
    monkeypatch: pytest.MonkeyPatch, orchestrator: Orchestrator, tmp_java_repo: Path
) -> None:
    def boom(component, hits):
        raise ExtractionError("LLM call failed: timeout")

    monkeypatch.setattr("agent.orchestrator.extract_from_docs", boom)

    request = ContextRequest(
        mode="branch",
        branch="feature/raise-daily-limit",
        base_branch="main",
        repo_path=tmp_java_repo,
    )
    with pytest.raises(OrchestratorError):
        orchestrator.answer(request)

    stats = orchestrator.graph.stats()
    # Step-by-step progress (_log_step) plus the final failure log both write
    # to `operations` — at least one row must record the actual error.
    assert stats["operations"] >= 1
    assert any(op["status"] == "error" for op in stats["recent_operations"])
