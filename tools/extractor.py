"""LLM-based extraction of business rules from repo docs and source code.

Two entry points, both pure (no disk/git/network I/O beyond the LLM call):
- `extract_from_docs`  — Camada 0: candidates from documentation already
  committed in the repo. Always produces unconfirmed candidates.
- `extract_from_code`  — Camada 2: extraction from real source code, which
  resolves whether prior candidates are confirmed, contradicted, or new.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator

from tools.docs_scanner import DocHit
from tools.openrouter import OpenRouterError, chat

logger = logging.getLogger("knowledge_agent")

RuleCategory = Literal["business", "technical"]
Confidence = Literal["high", "medium", "low"]
RuleOrigin = Literal["repo_doc", "explicit_tag", "inferred"]
RuleStatus = Literal["doc_only", "code_confirmed", "code_contradicts_doc", "code_only"]

RULE_ID_PATTERN = re.compile(r"^RN-\d{1,4}$")
# Ids alocados pelo próprio orchestrator (agent/orchestrator.py) quando o LLM
# não achou tag explícita no código/doc — precisam ser aceitos aqui também,
# já que regras reaproveitadas do cache voltam a passar por este validador.
AUTO_RULE_ID_PATTERN = re.compile(r"^RN-AUTO-\d{1,6}$")


class SourceLocation(BaseModel):
    file: str
    lines: str = ""


class ExtractedRule(BaseModel):
    rule_id: Optional[str] = None
    description: str
    condition: str = ""
    category: RuleCategory
    confidence: Confidence
    origin: RuleOrigin
    status: RuleStatus
    source_files: list[SourceLocation] = Field(default_factory=list)
    evidence: str = ""

    @field_validator("rule_id")
    @classmethod
    def _check_rule_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if not (RULE_ID_PATTERN.match(value) or AUTO_RULE_ID_PATTERN.match(value)):
            raise ValueError(
                f"rule_id must look like RN-XX or RN-AUTO-XX (got: {value!r})"
            )
        return value


TechnicalRefType = Literal[
    "table", "endpoint", "microservice", "class", "method",
    "function", "module", "script", "route", "component",
    "constraint", "sequence", "policy", "trigger",
]


class TechnicalRef(BaseModel):
    type: TechnicalRefType
    name: str
    description: str = ""
    source_files: list[SourceLocation] = Field(default_factory=list)


class CodeContext(BaseModel):
    component: str
    rules: list[ExtractedRule] = Field(default_factory=list)
    technical_refs: list[TechnicalRef] = Field(default_factory=list)
    summary: str = ""


class ExtractionError(RuntimeError):
    pass


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


_ALLOWED_REF_TYPES = set(get_args(TechnicalRefType))


def _drop_unrecognized_refs(payload: dict, *, context: str) -> dict:
    """A technical_ref with a type outside our closed enum is a labeling
    quirk (e.g. the LLM calling an HTML <script> tag "script"), not a reason
    to throw away the whole extraction — the rules are the valuable part.
    Dropped refs are logged, never silently lost from the audit trail."""
    refs = payload.get("technical_refs")
    if not isinstance(refs, list):
        return payload
    kept = []
    for ref in refs:
        ref_type = ref.get("type") if isinstance(ref, dict) else None
        if ref_type in _ALLOWED_REF_TYPES:
            kept.append(ref)
        else:
            logger.warning(
                "%s: dropping technical_ref with unrecognized type %r: %r",
                context, ref_type, ref,
            )
    payload["technical_refs"] = kept
    return payload


_INVALID_JSON_ESCAPE_PATTERN = re.compile(r'\\(?!["\\/bfnrtu])')


def _repair_invalid_escapes(text: str) -> str:
    """LLMs sometimes copy source text (e.g. 'R\\$ 5.000,00' from a
    markdown doc) straight into a JSON string without doubling the
    backslash, which breaks strict JSON parsing. Double any backslash that
    isn't already starting a valid JSON escape sequence."""
    return _INVALID_JSON_ESCAPE_PATTERN.sub(lambda m: "\\\\", text)


def _parse_code_context(raw: str, *, context: str) -> CodeContext:
    cleaned = _strip_code_fences(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        try:
            payload = json.loads(_repair_invalid_escapes(cleaned))
            logger.warning(
                "%s: LLM JSON had invalid backslash escapes, repaired and parsed anyway",
                context,
            )
        except json.JSONDecodeError:
            raise ExtractionError(
                f"{context}: LLM returned invalid JSON: {cleaned[:500]!r}"
            ) from exc
    if isinstance(payload, dict):
        payload = _drop_unrecognized_refs(payload, context=context)
    try:
        return CodeContext.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionError(
            f"{context}: LLM JSON failed schema validation: {exc}"
        ) from exc


# ------------------------------------------------------------ Camada 0: docs

SYSTEM_PROMPT_DOCS = (
    "Você é um investigador de regras de negócio de um sistema de pagamentos. "
    "Você lê documentação já existente no repositório (README, docs, ADRs, "
    "Javadoc) e extrai candidatos a regras de negócio. Documentação pode "
    "estar desatualizada — você NUNCA declara uma regra como confirmada, "
    "apenas como candidata. Você sempre responde com JSON puro, sem markdown "
    "e sem comentários."
)

USER_PROMPT_DOCS_TEMPLATE = """Componente: {component}

Analise os trechos de documentação abaixo (README, docs, ADRs, Javadoc) e \
extraia candidatos a regras de negócio e referências técnicas.

Schema esperado (JSON puro):
{{
  "component": "{component}",
  "rules": [
    {{
      "rule_id": "RN-XX ou null se não houver tag explícita no texto",
      "description": "descrição clara da regra",
      "condition": "condição ou critério, se explícita",
      "category": "business" | "technical",
      "confidence": "high" | "medium" | "low",
      "origin": "repo_doc",
      "status": "doc_only",
      "source_files": [{{"file": "caminho/do/doc.md", "lines": ""}}],
      "evidence": "trecho citado da documentação"
    }}
  ],
  "technical_refs": [
    {{
      "type": "table" | "endpoint" | "microservice" | "class" | "method",
      "name": "nome",
      "description": "para que serve",
      "source_files": [{{"file": "caminho/do/doc.md", "lines": ""}}]
    }}
  ],
  "summary": "resumo em 2-3 linhas do que a documentação descreve"
}}

Regras obrigatórias:
- "origin" é sempre "repo_doc" e "status" é sempre "doc_only" nesta extração \
— nunca "code_confirmed", pois nenhum código foi analisado ainda.
- Não invente regra que não esteja no texto.
- Escape corretamente barras invertidas e aspas dentro de strings JSON (ex: "R\\$ 100" precisa virar "R\\\\$ 100" no JSON).
- Retorne APENAS o JSON, sem explicações.

Documentação:
\"\"\"
{docs_text}
\"\"\""""


def _render_doc_hits(hits: list[DocHit]) -> str:
    parts = []
    for hit in hits:
        parts.append(
            f"[{hit.doc_type}] {hit.path} — {hit.heading}\n{hit.content}"
        )
    return "\n\n---\n\n".join(parts)


def extract_from_docs(component: str, hits: list[DocHit]) -> CodeContext:
    if not hits:
        return CodeContext(component=component, rules=[], technical_refs=[], summary="")

    prompt = USER_PROMPT_DOCS_TEMPLATE.format(
        component=component, docs_text=_render_doc_hits(hits)
    )
    try:
        raw = chat(
            system=SYSTEM_PROMPT_DOCS,
            user=prompt,
            temperature=0.1,
            response_format_json=True,
        )
    except OpenRouterError as exc:
        raise ExtractionError(f"extract_from_docs: LLM call failed: {exc}") from exc

    context = _parse_code_context(raw, context="extract_from_docs")

    # Defense in depth: Camada 0 output is never trusted as confirmed, no
    # matter what the LLM claims — override deterministically.
    forced_rules = [
        rule.model_copy(update={"origin": "repo_doc", "status": "doc_only"})
        for rule in context.rules
    ]
    return context.model_copy(update={"rules": forced_rules})


# ------------------------------------------------------- Camada 2: código

SYSTEM_PROMPT_CODE = (
    "Você é um investigador de regras de negócio implementadas em código-"
    "fonte real (qualquer linguagem — Java, JavaScript, HTML, SQL, etc). Sua "
    "prioridade é o que o código realmente faz — nunca invente regra que não "
    "esteja no trecho fornecido. Você compara o código real com candidatos "
    "vindos de documentação e do conhecimento já registrado para decidir se "
    "cada regra está confirmada, contradiz a documentação, ou é nova. Você "
    "sempre responde com JSON puro, sem markdown e sem comentários."
)

USER_PROMPT_CODE_TEMPLATE = """Componente: {component}
Modo de análise: {mode}

Regras já conhecidas para este componente (do grafo local, podem estar \
desatualizadas em relação ao código atual):
{known_rules_json}

Candidatos vindos de documentação do repositório (Camada 0, ainda não \
confirmados contra o código):
{doc_candidates_json}

Código-fonte a analisar:
{code_text}

Schema esperado (JSON puro):
{{
  "component": "{component}",
  "rules": [
    {{
      "rule_id": "RN-XX se houver tag explícita no código (comentário), senão null",
      "description": "descrição clara da regra, baseada no código real",
      "condition": "condição ou critério observado no código",
      "category": "business" | "technical",
      "confidence": "high" | "medium" | "low",
      "origin": "explicit_tag" | "inferred",
      "status": "code_confirmed" | "code_contradicts_doc" | "code_only",
      "source_files": [{{"file": "caminho/Arquivo.java", "lines": "42-58"}}],
      "evidence": "trecho de código citado, para auditoria"
    }}
  ],
  "technical_refs": [
    {{
      "type": "table" | "endpoint" | "microservice" | "class" | "method",
      "name": "nome",
      "description": "para que serve",
      "source_files": [{{"file": "caminho/Arquivo.java", "lines": ""}}]
    }}
  ],
  "summary": "resumo em 2-3 linhas do que este código implementa"
}}

Regras obrigatórias:
- "status" = "code_confirmed" quando o código confirma um candidato de doc \
ou regra já conhecida; "code_contradicts_doc" quando o código diverge do \
que a documentação/regra conhecida afirma (sinalize isso — é o achado mais \
valioso); "code_only" quando a regra não tinha candidato prévio.
- "origin" = "explicit_tag" só quando existir uma tag "RN-XX" explícita no \
código (comentário); caso contrário "inferred" e rule_id deve ser null.
- Não invente regra que não esteja evidenciada no código fornecido.
- Escape corretamente barras invertidas e aspas dentro de strings JSON (ex: "R\\$ 100" precisa virar "R\\\\$ 100" no JSON).
- Retorne APENAS o JSON, sem explicações.
"""


def _render_files(files: dict[str, str]) -> str:
    parts = []
    for path, content in files.items():
        parts.append(f"--- {path} ---\n{content}")
    return "\n\n".join(parts)


def extract_from_code(
    *,
    component: str,
    files: dict[str, str],
    mode: Literal["pr_diff", "full_flow"],
    known_rules: list,
    doc_candidates: list[ExtractedRule],
) -> CodeContext:
    if not files:
        return CodeContext(component=component, rules=[], technical_refs=[], summary="")

    known_rules_json = json.dumps(
        [
            {"rule_id": r.rule_id, "description": r.description, "status": r.status}
            for r in known_rules
        ],
        ensure_ascii=False,
        indent=2,
    )
    doc_candidates_json = json.dumps(
        [c.model_dump() for c in doc_candidates], ensure_ascii=False, indent=2
    )

    prompt = USER_PROMPT_CODE_TEMPLATE.format(
        component=component,
        mode=mode,
        known_rules_json=known_rules_json,
        doc_candidates_json=doc_candidates_json,
        code_text=_render_files(files),
    )

    try:
        raw = chat(
            system=SYSTEM_PROMPT_CODE,
            user=prompt,
            temperature=0.1,
            response_format_json=True,
        )
    except OpenRouterError as exc:
        raise ExtractionError(f"extract_from_code: LLM call failed: {exc}") from exc

    return _parse_code_context(raw, context="extract_from_code")
