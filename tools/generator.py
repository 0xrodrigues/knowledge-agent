"""LLM-based content generator for Confluence pages (XHTML storage format)."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable

from tools.extractor import ExtractedDocument
from tools.openrouter import OpenRouterError, chat


@dataclass(frozen=True)
class RelatedPageRef:
    product: str
    type: str
    title: str
    url: str


class GenerationError(RuntimeError):
    pass


SYSTEM_PROMPT = (
    "Você é um redator técnico especializado em documentação institucional. "
    "Você produz exclusivamente Confluence Storage Format (XHTML) válido, "
    "sem markdown, sem código JSON, sem explicações."
)

BUSINESS_TEMPLATE_HINT = """Template para páginas `business`:
<h1>[Nome do Produto]</h1>
<h2>O que é</h2>
<p>...</p>
<h2>Para quem se aplica</h2>
<p>...</p>
<h2>Regras de Negócio</h2>
<table><tbody>
<tr><th>ID</th><th>Descrição</th><th>Condição</th></tr>
<tr><td>RN-01</td><td>...</td><td>...</td></tr>
</tbody></table>
<h2>Casos de Uso</h2>
<p>...</p>
<h2>Referências Técnicas</h2>
<ul>...</ul>
<hr/>
<p><em>Última atualização: {timestamp} | Fonte: {source}</em></p>"""

TECHNICAL_TEMPLATE_HINT = """Template para páginas `technical`:
<h1>[Nome do Produto] — Documentação Técnica</h1>
<h2>Visão Geral do Fluxo</h2>
<p>...</p>
<h2>APIs e Endpoints</h2>
<table><tbody>
<tr><th>Endpoint</th><th>Método</th><th>Descrição</th></tr>
</tbody></table>
<h2>Tabelas Populadas</h2>
<table><tbody>
<tr><th>Tabela</th><th>Coluna</th><th>Descrição</th></tr>
</tbody></table>
<h2>Microserviços Envolvidos</h2>
<ul>...</ul>
<h2>Implementação de Regras de Negócio</h2>
<table><tbody>
<tr><th>Regra</th><th>Implementação Técnica</th></tr>
</tbody></table>
<h2>Referências de Negócio</h2>
<ul>...</ul>
<hr/>
<p><em>Última atualização: {timestamp} | Fonte: {source}</em></p>"""


USER_PROMPT_TEMPLATE = """Gere o conteúdo de uma página Confluence no formato Confluence Storage Format (XHTML).

Tipo de página: {type}
Produto: {product}
Fonte (nome do arquivo original): {source}
Timestamp de geração: {timestamp}

Dados extraídos (JSON):
{extracted_json}

Páginas relacionadas (use referências cruzadas onde apropriado):
{related_pages_json}

Use exatamente este template:
{template_hint}

Regras obrigatórias:
- Referências cruzadas DEVEM usar: <ac:link><ri:page ri:content-title="TÍTULO_DA_PÁGINA"/></ac:link>
  onde TÍTULO_DA_PÁGINA é o campo `title` de uma das páginas relacionadas.
- TODAS as regras RN-XX presentes nos dados extraídos devem aparecer na tabela de regras.
- Substitua os placeholders [Nome do Produto] pelo nome real.
- Não invente regras ou referências que não estão nos dados extraídos.
- Seja preciso, sem enrolação.
- Retorne APENAS o conteúdo XHTML, sem ```xml, sem ```html, sem comentários.
"""


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def generate_page_xhtml(
    *,
    extracted: ExtractedDocument,
    related_pages: Iterable[RelatedPageRef],
    source_filename: str,
    timestamp: str,
) -> str:
    template_hint = (
        BUSINESS_TEMPLATE_HINT
        if extracted.type == "business"
        else TECHNICAL_TEMPLATE_HINT
    )

    prompt = USER_PROMPT_TEMPLATE.format(
        type=extracted.type,
        product=extracted.product,
        source=source_filename,
        timestamp=timestamp,
        extracted_json=json.dumps(
            extracted.model_dump(), ensure_ascii=False, indent=2
        ),
        related_pages_json=json.dumps(
            [asdict(r) for r in related_pages], ensure_ascii=False, indent=2
        ),
        template_hint=template_hint,
    )

    try:
        raw = chat(system=SYSTEM_PROMPT, user=prompt)
    except OpenRouterError as exc:
        raise GenerationError(f"LLM call failed: {exc}") from exc

    cleaned = _strip_code_fences(raw)
    if not cleaned:
        raise GenerationError("Generator returned empty content.")
    return cleaned


def build_page_title(*, product: str, type_: str) -> str:
    if type_ == "business":
        return product
    if type_ == "technical":
        return f"{product} — Documentação Técnica"
    raise ValueError(f"Unknown page type: {type_!r}")
