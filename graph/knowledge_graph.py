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
class PageRecord:
    id: int
    product: str
    type: str
    confluence_page_id: str
    confluence_url: str
    last_updated: str


@dataclass(frozen=True)
class RuleRecord:
    id: int
    rule_id: str
    description: str
    product: str
    created_at: str


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

    def get_page(self, product: str, type_: str) -> Optional[PageRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pages WHERE product = ? AND type = ?",
                (product, type_),
            ).fetchone()
            return PageRecord(**dict(row)) if row else None

    def get_page_by_id(self, page_id: int) -> Optional[PageRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pages WHERE id = ?", (page_id,)
            ).fetchone()
            return PageRecord(**dict(row)) if row else None

    def list_pages_for_product(self, product: str) -> list[PageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pages WHERE product = ? ORDER BY type",
                (product,),
            ).fetchall()
            return [PageRecord(**dict(r)) for r in rows]

    def list_products(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT product FROM pages ORDER BY product"
            ).fetchall()
            return [r["product"] for r in rows]

    def get_rule(self, rule_id: str) -> Optional[RuleRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
            return RuleRecord(**dict(row)) if row else None

    def list_rules_for_product(self, product: str) -> list[RuleRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rules WHERE product = ? ORDER BY rule_id",
                (product,),
            ).fetchall()
            return [RuleRecord(**dict(r)) for r in rows]

    def dependent_pages(self, rule_id: str) -> list[PageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM pages p
                JOIN dependencies d ON d.page_id = p.id
                WHERE d.rule_id = ?
                """,
                (rule_id,),
            ).fetchall()
            return [PageRecord(**dict(r)) for r in rows]

    def stats(self) -> dict:
        with self._connect() as conn:
            pages = conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"]
            rules = conn.execute("SELECT COUNT(*) AS n FROM rules").fetchone()["n"]
            deps = conn.execute(
                "SELECT COUNT(*) AS n FROM dependencies"
            ).fetchone()["n"]
            ops = conn.execute(
                "SELECT COUNT(*) AS n FROM operations"
            ).fetchone()["n"]
            last_ops = conn.execute(
                "SELECT operation, status, timestamp, document_path "
                "FROM operations ORDER BY id DESC LIMIT 5"
            ).fetchall()
        return {
            "pages": pages,
            "rules": rules,
            "dependencies": deps,
            "operations": ops,
            "recent_operations": [dict(r) for r in last_ops],
        }

    # ----------------------------------------------------------------- writes

    def upsert_page(
        self,
        conn: sqlite3.Connection,
        *,
        product: str,
        type_: str,
        confluence_page_id: str,
        confluence_url: str,
    ) -> int:
        existing = conn.execute(
            "SELECT id FROM pages WHERE product = ? AND type = ?",
            (product, type_),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE pages
                   SET confluence_page_id = ?,
                       confluence_url = ?,
                       last_updated = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (confluence_page_id, confluence_url, existing["id"]),
            )
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO pages (product, type, confluence_page_id, confluence_url)
            VALUES (?, ?, ?, ?)
            """,
            (product, type_, confluence_page_id, confluence_url),
        )
        return int(cur.lastrowid)

    def upsert_rule(
        self,
        conn: sqlite3.Connection,
        *,
        rule_id: str,
        description: str,
        product: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO rules (rule_id, description, product)
            VALUES (?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                description = excluded.description,
                product = excluded.product
            """,
            (rule_id, description, product),
        )

    def link_rule_to_page(
        self, conn: sqlite3.Connection, *, rule_id: str, page_id: int
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO dependencies (rule_id, page_id)
            VALUES (?, ?)
            """,
            (rule_id, page_id),
        )

    def log_operation(
        self,
        conn: sqlite3.Connection,
        *,
        operation: str,
        document_path: Optional[str],
        pages_affected: Iterable[int],
        rules_affected: Iterable[str],
        status: str,
        detail: Optional[str] = None,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO operations
                (operation, document_path, pages_affected,
                 rules_affected, status, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                operation,
                document_path,
                json.dumps(list(pages_affected)),
                json.dumps(list(rules_affected)),
                status,
                detail,
            ),
        )
        return int(cur.lastrowid)
