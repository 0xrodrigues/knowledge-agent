"""Local SQLite-backed knowledge graph.

Public interface for reads + writes. Only the orchestrator should write;
other modules should treat this as read-only.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from config.settings import DB_PATH, SCHEMA_PATH


@dataclass(frozen=True)
class ComponentRecord:
    id: int
    name: str
    description: str
    last_full_scan_commit: Optional[str]
    last_full_scan_at: Optional[str]


@dataclass(frozen=True)
class RuleRecord:
    id: int
    rule_id: str
    component_id: int
    description: str
    condition: str
    category: str
    confidence: str
    origin: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RuleSourceRecord:
    id: int
    rule_id: str
    file_path: str
    line_range: str
    source_kind: str
    content_hash: str
    commit_sha: str
    last_verified_at: str


@dataclass(frozen=True)
class TechnicalRefRecord:
    id: int
    component_id: int
    type: str
    name: str
    description: str
    file_path: Optional[str]
    line_range: str
    updated_at: str


class KnowledgeGraph:
    def __init__(self, db_path: Path = DB_PATH, schema_path: Path = SCHEMA_PATH):
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path)
        self._ensure_schema()

    # ------------------------------------------------------------------ infra

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self.schema_path.read_text(encoding="utf-8"))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open an explicit transaction. Caller writes; commit on success."""
        with self._connect() as conn:
            yield conn

    # ------------------------------------------------------------------ reads

    def get_component(self, name: str) -> Optional[ComponentRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM components WHERE name = ?", (name,)
            ).fetchone()
            return ComponentRecord(**dict(row)) if row else None

    def get_component_by_id(self, component_id: int) -> Optional[ComponentRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM components WHERE id = ?", (component_id,)
            ).fetchone()
            return ComponentRecord(**dict(row)) if row else None

    def list_components(self) -> list[ComponentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM components ORDER BY name"
            ).fetchall()
            return [ComponentRecord(**dict(r)) for r in rows]

    def get_rule(self, rule_id: str) -> Optional[RuleRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
            return RuleRecord(**dict(row)) if row else None

    def rules_for_component(self, component_name: str) -> list[RuleRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM rules r
                JOIN components c ON c.id = r.component_id
                WHERE c.name = ?
                ORDER BY r.rule_id
                """,
                (component_name,),
            ).fetchall()
            return [RuleRecord(**dict(r)) for r in rows]

    def rules_touching_files(self, paths: Iterable[str]) -> list[RuleRecord]:
        paths = list(paths)
        if not paths:
            return []
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in paths)
            rows = conn.execute(
                f"""
                SELECT DISTINCT r.* FROM rules r
                JOIN rule_sources rs ON rs.rule_id = r.rule_id
                WHERE rs.file_path IN ({placeholders})
                ORDER BY r.rule_id
                """,
                paths,
            ).fetchall()
            return [RuleRecord(**dict(r)) for r in rows]

    def sources_for_rule(self, rule_id: str) -> list[RuleSourceRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_sources WHERE rule_id = ? ORDER BY file_path",
                (rule_id,),
            ).fetchall()
            return [RuleSourceRecord(**dict(r)) for r in rows]

    def source_hash_for(self, rule_id: str, file_path: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT content_hash FROM rule_sources
                WHERE rule_id = ? AND file_path = ?
                """,
                (rule_id, file_path),
            ).fetchone()
            return row["content_hash"] if row else None

    def technical_refs_for_component(self, component_name: str) -> list[TechnicalRefRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM technical_refs t
                JOIN components c ON c.id = t.component_id
                WHERE c.name = ?
                ORDER BY t.type, t.name
                """,
                (component_name,),
            ).fetchall()
            return [TechnicalRefRecord(**dict(r)) for r in rows]

    def max_auto_rule_number(self, component_id: int) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT rule_id FROM rules WHERE component_id = ? AND rule_id LIKE 'RN-AUTO-%'",
                (component_id,),
            ).fetchall()
        best = 0
        for row in rows:
            suffix = row["rule_id"].rsplit("-", 1)[-1]
            if suffix.isdigit():
                best = max(best, int(suffix))
        return best

    def stats(self) -> dict:
        with self._connect() as conn:
            components = conn.execute(
                "SELECT COUNT(*) AS n FROM components"
            ).fetchone()["n"]
            rules = conn.execute("SELECT COUNT(*) AS n FROM rules").fetchone()["n"]
            refs = conn.execute(
                "SELECT COUNT(*) AS n FROM technical_refs"
            ).fetchone()["n"]
            ops = conn.execute(
                "SELECT COUNT(*) AS n FROM operations"
            ).fetchone()["n"]
            last_ops = conn.execute(
                "SELECT operation, status, timestamp, source_ref "
                "FROM operations ORDER BY id DESC LIMIT 5"
            ).fetchall()
        return {
            "components": components,
            "rules": rules,
            "technical_refs": refs,
            "operations": ops,
            "recent_operations": [dict(r) for r in last_ops],
        }

    # ----------------------------------------------------------------- writes

    def upsert_component(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        description: str = "",
        last_full_scan_commit: Optional[str] = None,
        mark_full_scan: bool = False,
    ) -> int:
        existing = conn.execute(
            "SELECT id FROM components WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            if mark_full_scan:
                conn.execute(
                    """
                    UPDATE components
                       SET description = ?,
                           last_full_scan_commit = ?,
                           last_full_scan_at = CURRENT_TIMESTAMP
                     WHERE id = ?
                    """,
                    (description, last_full_scan_commit, existing["id"]),
                )
            elif description:
                conn.execute(
                    "UPDATE components SET description = ? WHERE id = ?",
                    (description, existing["id"]),
                )
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO components (name, description, last_full_scan_commit, last_full_scan_at)
            VALUES (?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
            """,
            (name, description, last_full_scan_commit if mark_full_scan else None, mark_full_scan),
        )
        return int(cur.lastrowid)

    def upsert_rule(
        self,
        conn: sqlite3.Connection,
        *,
        rule_id: str,
        component_id: int,
        description: str,
        condition: str,
        category: str,
        confidence: str,
        origin: str,
        status: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO rules
                (rule_id, component_id, description, condition, category,
                 confidence, origin, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                component_id = excluded.component_id,
                description = excluded.description,
                condition = excluded.condition,
                category = excluded.category,
                confidence = excluded.confidence,
                origin = excluded.origin,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (rule_id, component_id, description, condition, category, confidence, origin, status),
        )

    def upsert_rule_source(
        self,
        conn: sqlite3.Connection,
        *,
        rule_id: str,
        file_path: str,
        line_range: str,
        source_kind: str,
        content_hash: str,
        commit_sha: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO rule_sources
                (rule_id, file_path, line_range, source_kind, content_hash, commit_sha)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id, file_path) DO UPDATE SET
                line_range = excluded.line_range,
                source_kind = excluded.source_kind,
                content_hash = excluded.content_hash,
                commit_sha = excluded.commit_sha,
                last_verified_at = CURRENT_TIMESTAMP
            """,
            (rule_id, file_path, line_range, source_kind, content_hash, commit_sha),
        )

    def upsert_technical_ref(
        self,
        conn: sqlite3.Connection,
        *,
        component_id: int,
        type_: str,
        name: str,
        description: str = "",
        file_path: Optional[str] = None,
        line_range: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT INTO technical_refs
                (component_id, type, name, description, file_path, line_range)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(component_id, type, name) DO UPDATE SET
                description = excluded.description,
                file_path = excluded.file_path,
                line_range = excluded.line_range,
                updated_at = CURRENT_TIMESTAMP
            """,
            (component_id, type_, name, description, file_path, line_range),
        )

    def log_operation(
        self,
        conn: sqlite3.Connection,
        *,
        operation: str,
        source_ref: Optional[str],
        components_affected: Iterable[str],
        rules_affected: Iterable[str],
        status: str,
        detail: Optional[str] = None,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO operations
                (operation, source_ref, components_affected,
                 rules_affected, status, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                operation,
                source_ref,
                json.dumps(list(components_affected)),
                json.dumps(list(rules_affected)),
                status,
                detail,
            ),
        )
        return int(cur.lastrowid)
