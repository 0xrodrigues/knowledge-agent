"""Tests for graph/knowledge_graph.py against a throwaway SQLite file."""
from __future__ import annotations

from pathlib import Path

import pytest

from graph.knowledge_graph import KnowledgeGraph

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "graph" / "schema.sql"


@pytest.fixture()
def graph(tmp_path: Path) -> KnowledgeGraph:
    return KnowledgeGraph(db_path=tmp_path / "graph.db", schema_path=SCHEMA_PATH)


def _make_component(graph: KnowledgeGraph, name: str = "services/payments") -> int:
    with graph.transaction() as conn:
        component_id = graph.upsert_component(conn, name=name, description="Payments service")
    return component_id


def test_upsert_component_is_idempotent(graph: KnowledgeGraph) -> None:
    first_id = _make_component(graph)
    second_id = _make_component(graph)
    assert first_id == second_id
    assert len(graph.list_components()) == 1


def test_upsert_rule_and_lookup_by_component(graph: KnowledgeGraph) -> None:
    component_id = _make_component(graph)
    with graph.transaction() as conn:
        graph.upsert_rule(
            conn,
            rule_id="RN-001",
            component_id=component_id,
            description="Limite diario de transacao",
            condition="valor > cliente.limite_diario",
            category="business",
            confidence="high",
            origin="explicit_tag",
            status="code_confirmed",
        )

    rules = graph.rules_for_component("services/payments")
    assert len(rules) == 1
    assert rules[0].rule_id == "RN-001"
    assert rules[0].status == "code_confirmed"


def test_upsert_rule_update_changes_status(graph: KnowledgeGraph) -> None:
    component_id = _make_component(graph)
    with graph.transaction() as conn:
        graph.upsert_rule(
            conn,
            rule_id="RN-001",
            component_id=component_id,
            description="Limite diario",
            condition="",
            category="business",
            confidence="medium",
            origin="repo_doc",
            status="doc_only",
        )
    with graph.transaction() as conn:
        graph.upsert_rule(
            conn,
            rule_id="RN-001",
            component_id=component_id,
            description="Limite diario de transacao",
            condition="valor > cliente.limite_diario",
            category="business",
            confidence="high",
            origin="explicit_tag",
            status="code_confirmed",
        )

    rule = graph.get_rule("RN-001")
    assert rule is not None
    assert rule.status == "code_confirmed"
    assert rule.confidence == "high"


def test_rules_touching_files(graph: KnowledgeGraph) -> None:
    component_id = _make_component(graph)
    with graph.transaction() as conn:
        graph.upsert_rule(
            conn,
            rule_id="RN-001",
            component_id=component_id,
            description="Limite diario",
            condition="",
            category="business",
            confidence="high",
            origin="explicit_tag",
            status="code_confirmed",
        )
        graph.upsert_rule_source(
            conn,
            rule_id="RN-001",
            file_path="services/payments/limits.py",
            line_range="10-20",
            source_kind="code",
            content_hash="abc123",
            commit_sha="deadbeef",
        )

    hit = graph.rules_touching_files(["services/payments/limits.py"])
    assert len(hit) == 1
    assert hit[0].rule_id == "RN-001"
    assert graph.rules_touching_files(["nowhere.py"]) == []


def test_source_hash_staleness_detection(graph: KnowledgeGraph) -> None:
    component_id = _make_component(graph)
    with graph.transaction() as conn:
        graph.upsert_rule(
            conn,
            rule_id="RN-001",
            component_id=component_id,
            description="Limite diario",
            condition="",
            category="business",
            confidence="high",
            origin="explicit_tag",
            status="code_confirmed",
        )
        graph.upsert_rule_source(
            conn,
            rule_id="RN-001",
            file_path="limits.py",
            line_range="",
            source_kind="code",
            content_hash="original-hash",
            commit_sha="sha1",
        )

    assert graph.source_hash_for("RN-001", "limits.py") == "original-hash"

    with graph.transaction() as conn:
        graph.upsert_rule_source(
            conn,
            rule_id="RN-001",
            file_path="limits.py",
            line_range="",
            source_kind="code",
            content_hash="changed-hash",
            commit_sha="sha2",
        )
    assert graph.source_hash_for("RN-001", "limits.py") == "changed-hash"


def test_max_auto_rule_number(graph: KnowledgeGraph) -> None:
    component_id = _make_component(graph)
    assert graph.max_auto_rule_number(component_id) == 0
    with graph.transaction() as conn:
        graph.upsert_rule(
            conn,
            rule_id="RN-AUTO-3",
            component_id=component_id,
            description="x",
            condition="",
            category="technical",
            confidence="low",
            origin="inferred",
            status="code_only",
        )
        graph.upsert_rule(
            conn,
            rule_id="RN-AUTO-7",
            component_id=component_id,
            description="y",
            condition="",
            category="technical",
            confidence="low",
            origin="inferred",
            status="code_only",
        )
    assert graph.max_auto_rule_number(component_id) == 7


def test_technical_ref_upsert(graph: KnowledgeGraph) -> None:
    component_id = _make_component(graph)
    with graph.transaction() as conn:
        graph.upsert_technical_ref(
            conn,
            component_id=component_id,
            type_="endpoint",
            name="POST /payments/authorize",
            description="Autoriza pagamento",
            file_path="services/payments/api.py",
        )
    refs = graph.technical_refs_for_component("services/payments")
    assert len(refs) == 1
    assert refs[0].type == "endpoint"


def test_operations_are_always_logged_including_errors(graph: KnowledgeGraph) -> None:
    with graph.transaction() as conn:
        graph.log_operation(
            conn,
            operation="pr_context",
            source_ref="pr:482",
            components_affected=["services/payments"],
            rules_affected=["RN-001"],
            status="error",
            detail="RuntimeError: LLM call failed",
        )
    stats = graph.stats()
    assert stats["operations"] == 1
    assert stats["recent_operations"][0]["status"] == "error"


def test_transaction_rolls_back_on_exception(graph: KnowledgeGraph) -> None:
    with pytest.raises(RuntimeError):
        with graph.transaction() as conn:
            graph.upsert_component(conn, name="services/payments")
            raise RuntimeError("boom")
    assert graph.list_components() == []
