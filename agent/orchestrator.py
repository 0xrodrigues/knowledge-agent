"""Orchestrator: the only module that coordinates and writes to the graph.

Answers "what business rules live in this code?" in three layers:
  Camada 0 — docs already committed in the repo (cheap, no code read)
  Camada 1 — local graph cache (no LLM; skips files whose content hash
             already matches a stored, code-confirmed rule)
  Camada 2 — real source code (LLM), which resolves doc candidates into
             code_confirmed / code_contradicts_doc / code_only
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from config.settings import (
    CALL_CHAIN_DEPTH_DEFAULT,
    LLM_MAX_CONCURRENCY,
    LOG_PATH,
    MAX_FILES_PER_EXTRACTION,
    REPO_CACHE_DIR,
)
from graph.knowledge_graph import KnowledgeGraph, RuleRecord
from tools import code_scanner, docs_scanner, github, vcs
from tools.extractor import (
    ExtractedRule,
    ExtractionError,
    SourceLocation,
    TechnicalRef,
    extract_from_code,
    extract_from_docs,
)

logger = logging.getLogger("knowledge_agent")

_MAX_CHARS_PER_BATCH = 12_000


def _setup_file_logger() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(stream)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class OrchestratorError(RuntimeError):
    pass


RequestMode = Literal["pr_number", "branch", "area"]


@dataclass(frozen=True)
class ContextRequest:
    mode: RequestMode
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    base_branch: str = "main"
    area: Optional[str] = None
    repo_path: Optional[Path] = None
    repo: Optional[str] = None
    full_flow: bool = False
    call_chain_depth: int = CALL_CHAIN_DEPTH_DEFAULT


@dataclass(frozen=True)
class ContextAnswer:
    component: str
    origin_description: str
    coverage: Literal["full", "partial"]
    coverage_reason: str
    rules: list[ExtractedRule] = field(default_factory=list)
    technical_refs: list[TechnicalRef] = field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True)
class _ResolvedOrigin:
    origin_description: str
    seed_files: list[str]
    head_ref: str
    read_file: Callable[[str], str]
    repo_root: Optional[Path]
    truncated: bool = False


class Orchestrator:
    def __init__(self, graph: Optional[KnowledgeGraph] = None) -> None:
        _setup_file_logger()
        self.graph = graph or KnowledgeGraph()

    # --------------------------------------------------------------- public

    def answer(self, request: ContextRequest) -> ContextAnswer:
        self._validate_request(request)
        try:
            return self._answer_inner(request)
        except Exception as exc:
            self._log_failure(request, exc)
            logger.exception("Context request failed: %s", request)
            raise

    # ------------------------------------------------------------- internal

    def _validate_request(self, request: ContextRequest) -> None:
        has_local_source = request.repo_path is not None or bool(request.repo)

        if request.mode == "pr_number":
            if request.pr_number is None:
                raise OrchestratorError("mode='pr_number' requires pr_number.")
            if not has_local_source:
                raise OrchestratorError(
                    "mode='pr_number' requires either repo_path (local clone) "
                    "or repo ('owner/name' or a GitHub URL — the agent clones "
                    "it automatically)."
                )
        elif request.mode == "branch":
            if not request.branch:
                raise OrchestratorError("mode='branch' requires branch.")
            if not has_local_source:
                raise OrchestratorError(
                    "mode='branch' requires repo_path or repo (the agent "
                    "clones automatically when only repo is given)."
                )
        elif request.mode == "area":
            if not request.area:
                raise OrchestratorError("mode='area' requires area.")
            if not has_local_source:
                raise OrchestratorError(
                    "mode='area' requires repo_path or repo (the agent "
                    "clones automatically when only repo is given)."
                )
        else:
            raise OrchestratorError(f"Unknown request mode: {request.mode!r}")
        # Every mode above already requires repo_path or repo — full_flow
        # rides on that same local source (auto-cloned when only repo is given).

    def _ensure_repo_path(self, request: ContextRequest) -> Path:
        if request.repo_path is not None:
            return request.repo_path
        if not request.repo:
            raise OrchestratorError(
                "No repo_path and no repo given — cannot reach the code."
            )
        try:
            normalized = github.normalize_repo(request.repo)
            clone_url = github.clone_url_for(request.repo)
        except github.GithubError as exc:
            raise OrchestratorError(f"Invalid repo {request.repo!r}: {exc}") from exc

        dest = REPO_CACHE_DIR / normalized.replace("/", "__")
        try:
            return vcs.clone_or_update(clone_url, dest)
        except vcs.VcsError as exc:
            raise OrchestratorError(
                f"Failed to clone/update {request.repo}: {exc}"
            ) from exc

    def _resolve_origin(self, request: ContextRequest) -> _ResolvedOrigin:
        if request.mode == "pr_number":
            return self._resolve_pr(request)
        if request.mode == "branch":
            return self._resolve_branch(request)
        return self._resolve_area(request)

    def _resolve_pr(self, request: ContextRequest) -> _ResolvedOrigin:
        assert request.pr_number is not None
        # full_flow needs a local checkout (call-chain expansion, docs scan)
        # — auto-clone when the caller only gave `repo`.
        wants_local = request.repo_path is not None or request.full_flow
        if wants_local:
            if not request.repo:
                raise OrchestratorError(
                    "mode='pr_number' with a local checkout also requires "
                    "repo ('owner/name') to fetch PR metadata."
                )
            repo_path = self._ensure_repo_path(request)
            try:
                meta = github.fetch_pr_metadata(request.repo, request.pr_number)
            except github.GithubError as exc:
                raise OrchestratorError(f"Failed to fetch PR metadata: {exc}") from exc
            try:
                diff = vcs.diff_branches(repo_path, meta.base_sha, meta.head_sha)
            except vcs.VcsError as exc:
                raise OrchestratorError(
                    f"Failed to diff PR #{request.pr_number} locally: {exc}"
                ) from exc
            seed_files = [f.path for f in diff.files if f.status != "deleted"]
            return _ResolvedOrigin(
                origin_description=f"PR #{request.pr_number} ({meta.head_ref} -> {meta.base_ref}), local diff",
                seed_files=seed_files,
                head_ref=meta.head_sha,
                read_file=lambda path: vcs.read_file_at_ref(repo_path, meta.head_sha, path),
                repo_root=repo_path,
            )

        assert request.repo is not None
        try:
            diff = github.fetch_pr_diff(request.repo, request.pr_number)
        except github.GithubError as exc:
            raise OrchestratorError(
                f"Failed to fetch PR #{request.pr_number} via GitHub API: {exc}"
            ) from exc
        seed_files = [f.path for f in diff.files if f.status != "deleted"]
        return _ResolvedOrigin(
            origin_description=f"PR #{request.pr_number} ({diff.head_ref} -> {diff.base_ref}), GitHub API",
            seed_files=seed_files,
            head_ref=diff.head_sha,
            read_file=lambda path: github.read_file_at_ref(request.repo, diff.head_sha, path),
            repo_root=None,
        )

    def _resolve_branch(self, request: ContextRequest) -> _ResolvedOrigin:
        assert request.branch
        repo_path = self._ensure_repo_path(request)
        try:
            diff = vcs.diff_branches(repo_path, request.base_branch, request.branch)
        except vcs.VcsError as exc:
            raise OrchestratorError(
                f"Failed to diff '{request.base_branch}...{request.branch}': {exc}"
            ) from exc
        seed_files = [f.path for f in diff.files if f.status != "deleted"]
        return _ResolvedOrigin(
            origin_description=f"branch {request.branch} -> {request.base_branch}, local diff",
            seed_files=seed_files,
            head_ref=diff.head_sha,
            read_file=lambda path: vcs.read_file_at_ref(repo_path, diff.head_sha, path),
            repo_root=repo_path,
        )

    def _resolve_area(self, request: ContextRequest) -> _ResolvedOrigin:
        assert request.area
        repo_path = self._ensure_repo_path(request)
        try:
            head_sha = vcs.resolve_sha(repo_path, "HEAD")
            all_files = vcs.list_files_in_area(repo_path, request.area)
        except vcs.VcsError as exc:
            raise OrchestratorError(
                f"Failed to list files under '{request.area}': {exc}"
            ) from exc
        truncated = len(all_files) > MAX_FILES_PER_EXTRACTION
        seed_files = all_files[:MAX_FILES_PER_EXTRACTION]
        return _ResolvedOrigin(
            origin_description=f"area {request.area} @ {head_sha[:12]}",
            seed_files=seed_files,
            head_ref=head_sha,
            read_file=lambda path: vcs.read_file_at_ref(repo_path, head_sha, path),
            repo_root=repo_path,
            truncated=truncated,
        )

    def _derive_component(self, request: ContextRequest, seed_files: list[str]) -> str:
        if request.area:
            return request.area
        if not seed_files:
            return "root"
        if len(seed_files) == 1:
            parent = str(Path(seed_files[0]).parent)
            return parent if parent != "." else "root"
        try:
            import os

            common = os.path.commonpath(seed_files)
        except ValueError:
            common = ""
        return common or "root"

    def _answer_inner(self, request: ContextRequest) -> ContextAnswer:
        self._log_step(request=request, step="resolve_origin", status="in_progress")
        try:
            origin = self._resolve_origin(request)
        except Exception as exc:
            self._log_step(
                request=request,
                step="resolve_origin",
                status="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        component = self._derive_component(request, origin.seed_files)
        self._log_step(
            request=request,
            step="resolve_origin",
            status="success",
            detail=f"seed_files={len(origin.seed_files)}; head_ref={origin.head_ref}",
            component=component,
        )

        # Camada 0 — doc already committed in the repo
        doc_rules: list[ExtractedRule] = []
        doc_summary = ""
        doc_content_by_path: dict[str, str] = {}
        if origin.repo_root is not None:
            self._log_step(
                request=request, step="docs_scan", status="in_progress", component=component
            )
            try:
                doc_hits = docs_scanner.find_repo_docs(
                    origin.repo_root, area=request.area
                )
            except FileNotFoundError as exc:
                self._log_step(
                    request=request,
                    step="docs_scan",
                    status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                    component=component,
                )
                raise OrchestratorError(f"Failed to scan repo docs: {exc}") from exc
            try:
                doc_context = extract_from_docs(component, doc_hits)
            except ExtractionError as exc:
                self._log_step(
                    request=request,
                    step="docs_scan",
                    status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                    component=component,
                )
                raise OrchestratorError(f"Camada 0 (docs) extraction failed: {exc}") from exc
            doc_rules = doc_context.rules
            doc_summary = doc_context.summary
            doc_content_by_path = {hit.path: hit.content for hit in doc_hits}
            self._log_step(
                request=request,
                step="docs_scan",
                status="success",
                detail=f"doc_hits={len(doc_hits)}; doc_rules={len(doc_rules)}",
                component=component,
            )

        # Camada 1 — local cache: read seed files once, hash them, decide
        # which need Camada 2 re-processing.
        content_by_file: dict[str, str] = {}
        for path in origin.seed_files:
            try:
                content_by_file[path] = origin.read_file(path)
            except (vcs.VcsError, github.GithubError) as exc:
                raise OrchestratorError(f"Failed to read '{path}': {exc}") from exc

        known_rules = self.graph.rules_for_component(component)
        known_rules += [
            r for r in self.graph.rules_touching_files(origin.seed_files)
            if r.rule_id not in {k.rule_id for k in known_rules}
        ]

        files_needing_scan = self._files_needing_reprocessing(content_by_file, known_rules)

        # Camada 2 — real source code, only for what Camada 1 could not confirm.
        expanded = None
        if request.full_flow and origin.repo_root is not None:
            self._log_step(
                request=request, step="code_scan", status="in_progress", component=component
            )
            try:
                expanded = code_scanner.expand_call_chain(
                    origin.repo_root,
                    origin.seed_files,
                    max_depth=request.call_chain_depth,
                    max_files=MAX_FILES_PER_EXTRACTION,
                )
            except vcs.VcsError as exc:
                self._log_step(
                    request=request,
                    step="code_scan",
                    status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                    component=component,
                )
                raise OrchestratorError(f"Call-chain expansion failed: {exc}") from exc
            self._log_step(
                request=request,
                step="code_scan",
                status="success",
                detail=(
                    f"related_files={len(expanded.related_files)}; "
                    f"depth_used={expanded.depth_used}; truncated={expanded.truncated}"
                ),
                component=component,
            )
            for path in expanded.related_files:
                if path in content_by_file:
                    continue
                try:
                    content_by_file[path] = origin.read_file(path)
                except (vcs.VcsError, github.GithubError):
                    continue  # best-effort expansion; missing file just isn't included
            extra_known = self.graph.rules_touching_files(expanded.related_files)
            for r in extra_known:
                if r.rule_id not in {k.rule_id for k in known_rules}:
                    known_rules.append(r)
            files_needing_scan += self._files_needing_reprocessing(
                {p: content_by_file[p] for p in expanded.related_files if p in content_by_file},
                known_rules,
            )

        code_rules: list[ExtractedRule] = []
        code_technical_refs: list[TechnicalRef] = []
        summary = ""
        mode_label: Literal["pr_diff", "full_flow"] = (
            "full_flow" if request.full_flow else "pr_diff"
        )

        files_to_process = {p: content_by_file[p] for p in files_needing_scan}
        batches = code_scanner.chunk_for_llm(files_to_process, max_chars=_MAX_CHARS_PER_BATCH)
        if batches:
            logger.info(
                "Camada 2: %d arquivo(s) precisam de extracao via LLM, em %d lote(s)",
                len(files_to_process), len(batches),
            )
            self._log_step(
                request=request,
                step="llm_extract",
                status="in_progress",
                detail=f"files={len(files_to_process)}; batches={len(batches)}",
                component=component,
            )
        def _run_batch(i: int, batch: dict[str, str]):
            logger.info(
                "Camada 2: lote %d/%d iniciado (%d arquivo(s): %s)",
                i, len(batches), len(batch), ", ".join(batch.keys()),
            )
            batch_start = time.monotonic()
            batch_context = extract_from_code(
                component=component,
                files=batch,
                mode=mode_label,
                known_rules=known_rules,
                doc_candidates=doc_rules,
            )
            logger.info(
                "Camada 2: lote %d/%d concluido em %.1fs (%d regra(s), %d ref(s))",
                i, len(batches), time.monotonic() - batch_start,
                len(batch_context.rules), len(batch_context.technical_refs),
            )
            return i, batch_context

        # Batches são chamadas LLM independentes (I/O-bound) — rodar em
        # paralelo com um pool limitado corta o tempo total drasticamente
        # sem estourar rate limit do OpenRouter. Todas em voo terminam antes
        # de propagar erro, pra não desperdiçar chamadas já pagas/em curso.
        first_error: Optional[tuple[int, Exception]] = None
        if batches:
            worker_count = min(LLM_MAX_CONCURRENCY, len(batches))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {
                    pool.submit(_run_batch, i, batch): i
                    for i, batch in enumerate(batches, start=1)
                }
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        _, batch_context = future.result()
                    except ExtractionError as exc:
                        if first_error is None:
                            first_error = (i, exc)
                        continue
                    code_rules.extend(batch_context.rules)
                    code_technical_refs.extend(batch_context.technical_refs)
                    if not summary and batch_context.summary:
                        summary = batch_context.summary

        if first_error is not None:
            i, exc = first_error
            self._log_step(
                request=request,
                step="llm_extract",
                status="error",
                detail=f"batch={i}/{len(batches)}; {type(exc).__name__}: {exc}",
                component=component,
            )
            raise OrchestratorError(f"Camada 2 (code) extraction failed: {exc}") from exc

        if batches:
            self._log_step(
                request=request,
                step="llm_extract",
                status="success",
                detail=f"code_rules={len(code_rules)}; technical_refs={len(code_technical_refs)}",
                component=component,
            )

        # Deterministic technical_refs straight from Java annotations — free, no LLM.
        for path, content in files_to_process.items():
            if not path.endswith(".java"):
                continue
            symbols = code_scanner.extract_symbols(path, content)
            code_technical_refs.extend(code_scanner.infer_technical_refs(symbols))

        checked_files = set(content_by_file.keys())
        reused_rules = self._reused_confirmed_rules(
            known_rules, checked_files, set(files_needing_scan)
        )

        final_rules = self._merge_rules(reused_rules, code_rules, doc_rules, files_to_process)
        final_refs = self._dedup_refs(
            code_technical_refs + self._graph_refs(component)
        )

        coverage, coverage_reason = self._compute_coverage(
            request=request, component=component, origin=origin, expanded=expanded
        )

        if not summary:
            summary = doc_summary
        if not summary:
            summary = f"{len(final_rules)} regra(s) de negocio encontrada(s) para {component}."

        mark_full_scan = (
            request.full_flow
            and not origin.truncated
            and (expanded is None or not expanded.truncated)
        )
        resolved_rules = self._persist(
            request=request,
            component=component,
            origin=origin,
            final_rules=final_rules,
            final_refs=final_refs,
            content_by_file=content_by_file,
            doc_content_by_path=doc_content_by_path,
            mark_full_scan=mark_full_scan,
        )

        return ContextAnswer(
            component=component,
            origin_description=origin.origin_description,
            coverage=coverage,
            coverage_reason=coverage_reason,
            rules=resolved_rules,
            technical_refs=final_refs,
            summary=summary,
        )

    # ------------------------------------------------------- Camada 1 logic

    def _files_needing_reprocessing(
        self, content_by_file: dict[str, str], known_rules: list[RuleRecord]
    ) -> list[str]:
        needing: list[str] = []
        for path, content in content_by_file.items():
            current_hash = _content_hash(content)
            fresh = False
            for rule in known_rules:
                if rule.status != "code_confirmed":
                    continue
                for source in self.graph.sources_for_rule(rule.rule_id):
                    if (
                        source.file_path == path
                        and source.source_kind == "code"
                        and source.content_hash == current_hash
                    ):
                        fresh = True
                        break
                if fresh:
                    break
            if not fresh:
                needing.append(path)
        return needing

    def _reused_confirmed_rules(
        self,
        known_rules: list[RuleRecord],
        checked_files: set[str],
        files_needing_scan: set[str],
    ) -> list[ExtractedRule]:
        """Rules whose every source file was read AND confirmed fresh this
        run — reused as-is, without spending an LLM call on them. A rule
        whose source file was never read this run is NOT reused: we cannot
        vouch for it just because nothing said otherwise."""
        reused: list[ExtractedRule] = []
        for rule in known_rules:
            if rule.status != "code_confirmed":
                continue
            sources = self.graph.sources_for_rule(rule.rule_id)
            if not sources:
                continue
            source_paths = {s.file_path for s in sources}
            if not source_paths.issubset(checked_files):
                continue
            if source_paths & files_needing_scan:
                continue
            reused.append(
                ExtractedRule(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    condition=rule.condition,
                    category=rule.category,  # type: ignore[arg-type]
                    confidence=rule.confidence,  # type: ignore[arg-type]
                    origin=rule.origin,  # type: ignore[arg-type]
                    status=rule.status,  # type: ignore[arg-type]
                    source_files=[
                        SourceLocation(file=s.file_path, lines=s.line_range)
                        for s in sources
                    ],
                    evidence="(reaproveitado do cache — hash de conteudo inalterado)",
                )
            )
        return reused

    def _merge_rules(
        self,
        reused: list[ExtractedRule],
        code_rules: list[ExtractedRule],
        doc_rules: list[ExtractedRule],
        files_processed: dict[str, str],
    ) -> list[ExtractedRule]:
        by_key: dict[str, ExtractedRule] = {}

        def _key(rule: ExtractedRule) -> str:
            if rule.rule_id:
                return rule.rule_id
            return _normalize(rule.description)

        for rule in reused:
            by_key[_key(rule)] = rule
        for rule in code_rules:
            by_key[_key(rule)] = rule  # code extraction always wins over reuse/doc

        processed_files = set(files_processed.keys())
        for rule in doc_rules:
            key = _key(rule)
            if key in by_key:
                continue  # already resolved by code extraction this run
            was_checked = any(
                sf.file in processed_files for sf in rule.source_files
            )
            if was_checked:
                continue  # code was scanned but didn't surface this — drop stale doc claim
            by_key[key] = rule

        return list(by_key.values())

    def _graph_refs(self, component: str) -> list[TechnicalRef]:
        return [
            TechnicalRef(
                type=r.type,  # type: ignore[arg-type]
                name=r.name,
                description=r.description,
                source_files=[SourceLocation(file=r.file_path or "", lines=r.line_range)],
            )
            for r in self.graph.technical_refs_for_component(component)
        ]

    def _dedup_refs(self, refs: list[TechnicalRef]) -> list[TechnicalRef]:
        seen: dict[tuple[str, str], TechnicalRef] = {}
        for ref in refs:
            seen[(ref.type, ref.name)] = ref
        return list(seen.values())

    # ----------------------------------------------------------- coverage

    def _compute_coverage(
        self,
        *,
        request: ContextRequest,
        component: str,
        origin: _ResolvedOrigin,
        expanded: Optional[code_scanner.ExpandedScope],
    ) -> tuple[Literal["full", "partial"], str]:
        if origin.truncated:
            return (
                "partial",
                "A listagem de arquivos da area foi truncada pelo limite de "
                f"{MAX_FILES_PER_EXTRACTION} arquivos; pode haver regra em "
                "arquivo nao analisado.",
            )

        if request.full_flow:
            if expanded is not None and expanded.truncated:
                return (
                    "partial",
                    f"Expansao de cadeia de chamadas truncada em profundidade "
                    f"{expanded.depth_used} pelo limite de {MAX_FILES_PER_EXTRACTION} "
                    "arquivos; pode haver regra em arquivo nao alcancado.",
                )
            return (
                "full",
                "Fluxo completo analisado: diff/area + cadeia de chamadas "
                "ate a profundidade configurada, sem truncamento.",
            )

        existing = self.graph.get_component(component)
        if existing and existing.last_full_scan_commit == origin.head_ref:
            return (
                "full",
                "Componente ja teve um scan de fluxo completo neste commit; "
                "nada relevante mudou desde entao.",
            )

        return (
            "partial",
            "Analise restrita ao diff/PR. Nenhum scan de fluxo completo foi "
            "feito para este componente nesta consulta — regras podem existir "
            "em codigo nao tocado por este PR.",
        )

    # --------------------------------------------------------------- write

    def _persist(
        self,
        *,
        request: ContextRequest,
        component: str,
        origin: _ResolvedOrigin,
        final_rules: list[ExtractedRule],
        final_refs: list[TechnicalRef],
        content_by_file: dict[str, str],
        doc_content_by_path: dict[str, str],
        mark_full_scan: bool,
    ) -> list[ExtractedRule]:
        """Writes everything to the graph and returns final_rules with
        rule_id resolved (RN-AUTO-<n> allocated where missing) — the caller
        must use this return value, not the original final_rules, otherwise
        the answer shown to the engineer still has unresolved rule_ids."""
        operation = "flow_context" if request.full_flow else "pr_context"
        source_ref = self._source_ref_label(request)
        resolved_rules: list[ExtractedRule] = []

        with self.graph.transaction() as conn:
            component_id = self.graph.upsert_component(
                conn,
                name=component,
                last_full_scan_commit=origin.head_ref if mark_full_scan else None,
                mark_full_scan=mark_full_scan,
            )

            existing_by_normalized = {
                _normalize(r.description): r.rule_id
                for r in self.graph.rules_for_component(component)
            }
            next_auto = self.graph.max_auto_rule_number(component_id)

            for rule in final_rules:
                rule_id = rule.rule_id
                if not rule_id:
                    normalized = _normalize(rule.description)
                    rule_id = existing_by_normalized.get(normalized)
                    if not rule_id:
                        next_auto += 1
                        rule_id = f"RN-AUTO-{next_auto}"
                    existing_by_normalized[normalized] = rule_id
                    rule = rule.model_copy(update={"rule_id": rule_id})
                resolved_rules.append(rule)

                self.graph.upsert_rule(
                    conn,
                    rule_id=rule_id,
                    component_id=component_id,
                    description=rule.description,
                    condition=rule.condition,
                    category=rule.category,
                    confidence=rule.confidence,
                    origin=rule.origin,
                    status=rule.status,
                )
                source_kind = "doc" if rule.origin == "repo_doc" else "code"
                for source in rule.source_files:
                    if source_kind == "code":
                        file_content = content_by_file.get(source.file)
                    else:
                        file_content = doc_content_by_path.get(source.file)
                    # Fallback keeps the row auditable even when the file
                    # wasn't read this run (e.g. a doc source outside the
                    # scanned area) — it just never matches a future hash,
                    # so that source is always treated as needing a re-check.
                    hash_input = file_content if file_content is not None else (
                        f"unread:{source.file}:{rule.rule_id}"
                    )
                    self.graph.upsert_rule_source(
                        conn,
                        rule_id=rule_id,
                        file_path=source.file,
                        line_range=source.lines,
                        source_kind=source_kind,
                        content_hash=_content_hash(hash_input),
                        commit_sha=origin.head_ref,
                    )

            for ref in final_refs:
                self.graph.upsert_technical_ref(
                    conn,
                    component_id=component_id,
                    type_=ref.type,
                    name=ref.name,
                    description=ref.description,
                    file_path=ref.source_files[0].file if ref.source_files else None,
                    line_range=ref.source_files[0].lines if ref.source_files else "",
                )

            self.graph.log_operation(
                conn,
                operation=operation,
                source_ref=source_ref,
                components_affected=[component],
                rules_affected=[r.rule_id for r in resolved_rules if r.rule_id],
                status="success",
                detail=f"rules={len(resolved_rules)}; refs={len(final_refs)}",
            )

        logger.info(
            "Context answered: component=%s rules=%d refs=%d",
            component,
            len(resolved_rules),
            len(final_refs),
        )
        return resolved_rules

    def _source_ref_label(self, request: ContextRequest) -> str:
        if request.mode == "pr_number":
            return f"pr:{request.pr_number}"
        if request.mode == "branch":
            return f"branch:{request.branch}..{request.base_branch}"
        return f"area:{request.area}"

    def _log_failure(self, request: ContextRequest, exc: BaseException) -> None:
        try:
            with self.graph.transaction() as conn:
                self.graph.log_operation(
                    conn,
                    operation="flow_context" if request.full_flow else "pr_context",
                    source_ref=self._source_ref_label(request),
                    components_affected=[],
                    rules_affected=[],
                    status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
        except Exception:
            logger.exception("Also failed to log error to operations table.")

    def _log_step(
        self,
        *,
        request: ContextRequest,
        step: str,
        status: Literal["in_progress", "success", "error"],
        detail: str = "",
        component: Optional[str] = None,
    ) -> None:
        """Registers one phase of the flow (resolve_origin, docs_scan,
        code_scan, llm_extract) as its own row in `operations`, so `status`/
        `list` can show progress mid-run instead of only the final outcome.
        Logging failures here must never abort the actual context request."""
        operation = f"{'flow_context' if request.full_flow else 'pr_context'}:{step}"
        try:
            with self.graph.transaction() as conn:
                self.graph.log_operation(
                    conn,
                    operation=operation,
                    source_ref=self._source_ref_label(request),
                    components_affected=[component] if component else [],
                    rules_affected=[],
                    status=status,
                    detail=detail,
                )
        except Exception:
            logger.exception("Failed to log step '%s' to operations table.", step)
