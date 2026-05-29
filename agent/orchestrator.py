"""Orchestrator: the only module that coordinates and writes to the graph."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import LOG_PATH
from graph.knowledge_graph import KnowledgeGraph, PageRecord
from tools import parser
from tools.confluence import ConfluenceClient, ConfluencePage
from tools.extractor import ExtractedDocument, Rule, extract
from tools.generator import (
    RelatedPageRef,
    build_page_title,
    generate_page_xhtml,
)

logger = logging.getLogger("knowledge_agent")


def _setup_file_logger() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(stream)


_OPPOSITE_TYPE = {"business": "technical", "technical": "business"}


@dataclass
class IngestResult:
    document_path: str
    product: str
    primary_type: str
    operation: str  # 'create' | 'update'
    pages_touched: list[ConfluencePage]
    rules_seen: list[str]


class OrchestratorError(RuntimeError):
    pass


class Orchestrator:
    def __init__(
        self,
        graph: Optional[KnowledgeGraph] = None,
        confluence: Optional[ConfluenceClient] = None,
    ) -> None:
        _setup_file_logger()
        self.graph = graph or KnowledgeGraph()
        self._confluence = confluence

    @property
    def confluence(self) -> ConfluenceClient:
        if self._confluence is None:
            self._confluence = ConfluenceClient()
        return self._confluence

    # --------------------------------------------------------------- public

    def ingest_file(self, path: str | Path) -> IngestResult:
        document_path = str(Path(path).resolve())
        logger.info("Ingesting document: %s", document_path)
        try:
            raw_text = parser.parse(document_path)
            extracted = extract(raw_text)
            return self._process(extracted, document_path)
        except Exception as exc:
            self._log_failure(document_path, exc)
            logger.exception("Ingestion failed for %s", document_path)
            raise

    def ingest_folder(self, folder: str | Path) -> list[IngestResult]:
        folder_path = Path(folder)
        if not folder_path.is_dir():
            raise OrchestratorError(f"Not a directory: {folder_path}")
        results: list[IngestResult] = []
        for path in sorted(folder_path.iterdir()):
            if path.suffix.lower() not in parser.SUPPORTED_EXTENSIONS:
                continue
            try:
                results.append(self.ingest_file(path))
            except Exception:
                # Already logged inside ingest_file; keep going through the folder.
                continue
        return results

    # ------------------------------------------------------------- internal

    def _process(
        self, extracted: ExtractedDocument, document_path: str
    ) -> IngestResult:
        product = extracted.product
        primary_type = extracted.type
        opposite_type = _OPPOSITE_TYPE[primary_type]

        existing_primary = self.graph.get_page(product, primary_type)
        existing_opposite = self.graph.get_page(product, opposite_type)
        operation = "update" if existing_primary else "create"

        # Cross-link: build related-page refs *before* upserting so the LLM
        # knows the opposite-type page exists (titles are deterministic).
        opposite_title = build_page_title(product=product, type_=opposite_type)
        related_for_primary: list[RelatedPageRef] = []
        if existing_opposite:
            related_for_primary.append(
                RelatedPageRef(
                    product=product,
                    type=opposite_type,
                    title=opposite_title,
                    url=existing_opposite.confluence_url,
                )
            )
        else:
            related_for_primary.append(
                RelatedPageRef(
                    product=product,
                    type=opposite_type,
                    title=opposite_title,
                    url="",
                )
            )

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_filename = Path(document_path).name

        # --- Primary page generation + Confluence upsert ---------------------
        primary_xhtml = generate_page_xhtml(
            extracted=extracted,
            related_pages=related_for_primary,
            source_filename=source_filename,
            timestamp=timestamp,
        )
        primary_title = build_page_title(product=product, type_=primary_type)
        primary_page = self.confluence.upsert_page(
            title=primary_title,
            body_xhtml=primary_xhtml,
            type_=primary_type,
        )
        pages_touched: list[ConfluencePage] = [primary_page]

        # --- Cross-type cascade ---------------------------------------------
        # If a rule changes, the opposite-type page must be re-evaluated.
        rules_changed = self._rules_changed(extracted.rules, product)
        cascade_page: Optional[ConfluencePage] = None
        cascade_synthetic: Optional[ExtractedDocument] = None

        if rules_changed and existing_opposite:
            cascade_synthetic = self._synthesize_opposite_doc(
                product=product,
                opposite_type=opposite_type,
                primary_extracted=extracted,
            )
            cascade_related = [
                RelatedPageRef(
                    product=product,
                    type=primary_type,
                    title=primary_title,
                    url=primary_page.url,
                )
            ]
            cascade_xhtml = generate_page_xhtml(
                extracted=cascade_synthetic,
                related_pages=cascade_related,
                source_filename=source_filename,
                timestamp=timestamp,
            )
            cascade_page = self.confluence.upsert_page(
                title=opposite_title,
                body_xhtml=cascade_xhtml,
                type_=opposite_type,
            )
            pages_touched.append(cascade_page)

        # --- Graph write (single transaction) -------------------------------
        with self.graph.transaction() as conn:
            primary_id = self.graph.upsert_page(
                conn,
                product=product,
                type_=primary_type,
                confluence_page_id=primary_page.page_id,
                confluence_url=primary_page.url,
            )
            page_ids: list[int] = [primary_id]

            if cascade_page is not None:
                cascade_id = self.graph.upsert_page(
                    conn,
                    product=product,
                    type_=opposite_type,
                    confluence_page_id=cascade_page.page_id,
                    confluence_url=cascade_page.url,
                )
                page_ids.append(cascade_id)

            for rule in extracted.rules:
                self.graph.upsert_rule(
                    conn,
                    rule_id=rule.rule_id,
                    description=rule.description,
                    product=product,
                )
                for pid in page_ids:
                    self.graph.link_rule_to_page(
                        conn, rule_id=rule.rule_id, page_id=pid
                    )

            self.graph.log_operation(
                conn,
                operation=operation,
                document_path=document_path,
                pages_affected=page_ids,
                rules_affected=[r.rule_id for r in extracted.rules],
                status="success",
                detail=(
                    f"primary={primary_type}; cascade="
                    f"{'yes' if cascade_page else 'no'}"
                ),
            )

        logger.info(
            "Ingestion success: product=%s primary=%s cascade=%s rules=%d",
            product,
            primary_type,
            "yes" if cascade_page else "no",
            len(extracted.rules),
        )

        return IngestResult(
            document_path=document_path,
            product=product,
            primary_type=primary_type,
            operation=operation,
            pages_touched=pages_touched,
            rules_seen=[r.rule_id for r in extracted.rules],
        )

    # ---------------------------------------------------------------- helpers

    def _rules_changed(self, rules: list[Rule], product: str) -> bool:
        if not rules:
            return False
        existing = {
            r.rule_id: r.description
            for r in self.graph.list_rules_for_product(product)
        }
        for rule in rules:
            if rule.rule_id not in existing:
                return True
            if existing[rule.rule_id].strip() != rule.description.strip():
                return True
        return False

    def _synthesize_opposite_doc(
        self,
        *,
        product: str,
        opposite_type: str,
        primary_extracted: ExtractedDocument,
    ) -> ExtractedDocument:
        """Build a synthetic ExtractedDocument for the opposite-type page,
        merging the freshly-extracted rules with whatever the graph already
        knows about this product."""
        from tools.extractor import Rule as ExtractedRule

        known = {r.rule_id: r for r in self.graph.list_rules_for_product(product)}
        merged: dict[str, ExtractedRule] = {}
        for r in primary_extracted.rules:
            merged[r.rule_id] = r
        for rid, record in known.items():
            if rid not in merged:
                merged[rid] = ExtractedRule(
                    rule_id=rid, description=record.description, condition=""
                )

        # Preserve technical_refs only when the opposite is technical and the
        # primary is business; in the reverse direction we have no business
        # narrative to invent, so leave it empty and let the LLM render only
        # the rules table + cross-link.
        technical_refs = (
            primary_extracted.technical_refs
            if opposite_type == "technical"
            else []
        )

        return ExtractedDocument(
            product=product,
            type=opposite_type,  # type: ignore[arg-type]
            rules=list(merged.values()),
            technical_refs=technical_refs,
            summary=(
                f"Página gerada por cascata a partir de atualização em "
                f"'{primary_extracted.type}'."
            ),
        )

    def _log_failure(self, document_path: str, exc: BaseException) -> None:
        try:
            with self.graph.transaction() as conn:
                self.graph.log_operation(
                    conn,
                    operation="ingest",
                    document_path=document_path,
                    pages_affected=[],
                    rules_affected=[],
                    status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
        except Exception:
            # Never let the audit-logging error mask the original failure.
            logger.exception("Also failed to log error to operations table.")
