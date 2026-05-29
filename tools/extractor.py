"""LLM-based extractor: classify document type and pull rules/entities."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from tools.openrouter import OpenRouterError, chat

DocumentType = Literal["business", "technical"]
TechnicalRefType = Literal["table", "endpoint", "microservice", "class"]

RULE_ID_PATTERN = re.compile(r"^RN-\d{1,4}$")


class Rule(BaseModel):
    rule_id: str
    description: str
    condition: str = ""

    @field_validator("rule_id")
    @classmethod
    def _check_rule_id(cls, value: str) -> str:
        value = value.strip().upper()
        if not RULE_ID_PATTERN.match(value):
            raise ValueError(
                f"rule_id must look like RN-XX (got: {value!r})"
            )
        return value


class TechnicalRef(BaseModel):
    type: TechnicalRefType
    name: str
    description: str = ""


class ExtractedDocument(BaseModel):
    product: str = Field(min_length=1)
    type: DocumentType
    rules: list[Rule] = Field(default_factory=list)
    technical_refs: list[TechnicalRef] = Field(default_factory=list)
    summary: str = ""


class ExtractionError(RuntimeError):
    pass


SYSTEM_PROMPT = (
    "Você é um extrator de conhecimento institucional. "
    "Você sempre responde com JSON puro, sem markdown e sem comentários."
)

USER_PROMPT_TEMPLATE = """Analise o documento abaixo e retorne APENAS um JSON válido, sem markdown, sem explicações.

Schema esperado:
{{
  "product": "nome do produto principal do documento",
  "type": "business" | "technical",
  "rules": [
    {{
      "rule_id": "RN-XX",
      "description": "descrição clara da regra",
      "condition": "condição ou critério da regra"
    }}
  ],
  "technical_refs": [
    {{
      "type": "table" | "endpoint" | "microservice" | "class",
      "name": "nome",
      "description": "para que serve"
    }}
  ],
  "summary": "resumo do documento em 2-3 linhas"
}}

Regras de classificação:
- "business" = produto, requisitos funcionais, regras de negócio (RN-XX).
- "technical" = endpoints, classes, tabelas, microsserviços, fluxos de sistema.

Documento:
\"\"\"
{document}
\"\"\""""


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove leading ```lang and trailing ```
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


def extract(document_text: str) -> ExtractedDocument:
    if not document_text.strip():
        raise ExtractionError("Empty document text passed to extractor.")

    prompt = USER_PROMPT_TEMPLATE.format(document=document_text)

    try:
        raw = chat(
            system=SYSTEM_PROMPT,
            user=prompt,
            response_format_json=True,
        )
    except OpenRouterError as exc:
        raise ExtractionError(f"LLM call failed: {exc}") from exc

    cleaned = _strip_code_fences(raw)

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"Extractor returned invalid JSON: {cleaned[:500]!r}"
        ) from exc

    try:
        return ExtractedDocument.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionError(
            f"Extractor JSON failed schema validation: {exc}"
        ) from exc
