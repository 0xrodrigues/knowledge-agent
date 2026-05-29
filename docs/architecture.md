# Arquitetura — Knowledge Agent

## 1. Visão Geral

O Knowledge Agent é um pipeline Python que transforma documentos institucionais (PDF/Word) em páginas estruturadas no Confluence, mantendo um grafo local de regras, páginas e dependências em SQLite.

O agente é o **único escritor** do espaço Confluence configurado. Nenhuma edição manual é assumida ou tolerada.

```
documento → parser → extractor (LLM) → grafo (lookup) → generator (LLM) → confluence (upsert) → grafo (write+log)
```

---

## 2. Princípios de Design

| Princípio | Como é garantido |
|-----------|------------------|
| **Atomicidade** | Todas as escritas no grafo ocorrem em uma única transação SQLite por ingestão. Falha em qualquer etapa → rollback completo. |
| **Idempotência** | Constraints `UNIQUE` em `pages(product,type)`, `rules(rule_id)`, `dependencies(rule_id,page_id)` + `ON CONFLICT DO UPDATE`. Reprocessar o mesmo documento atualiza em vez de duplicar. |
| **Auditabilidade** | Toda operação (sucesso ou erro) é gravada na tabela `operations` e em `logs/operations.log`. |
| **Falha explícita** | Exceções tipadas por camada (`UnsupportedDocumentError`, `OpenRouterError`, `ExtractionError`, `GenerationError`, `ConfluenceError`, `OrchestratorError`). Nada é silenciado. |
| **Único coordenador** | `agent/orchestrator.py` é o único módulo que lê *e* escreve no grafo. Demais módulos não tocam SQLite. |

---

## 3. Estrutura de Pastas

```
knowledge-agent/
├── config/
│   └── settings.py          # Carrega .env, expõe constantes + require_* guards
├── tools/
│   ├── parser.py            # PDF (pymupdf) / DOCX (python-docx) → texto bruto
│   ├── openrouter.py        # Cliente HTTP para OpenRouter (httpx)
│   ├── extractor.py         # LLM: classifica tipo, extrai produto, RN-XX, refs técnicas
│   ├── generator.py         # LLM: gera XHTML Confluence Storage Format
│   └── confluence.py        # atlassian-python-api: upsert de páginas
├── graph/
│   ├── knowledge_graph.py   # Interface SQLite (reads públicos, writes via transaction)
│   └── schema.sql           # DDL das tabelas + índices
├── agent/
│   └── orchestrator.py      # Único coordenador do fluxo; único escritor do grafo
├── data/graph.db            # SQLite local (auto-gerado)
├── logs/operations.log      # Auditoria estruturada
├── docs/architecture.md     # Este documento
├── main.py                  # CLI: ingest / status / list
├── requirements.txt
└── .env.example
```

---

## 4. Camadas e Responsabilidades

### 4.1 `config/settings.py`
- Lê variáveis do `.env` (dotenv é opcional em tempo de import).
- Define caminhos absolutos: `DB_PATH`, `SCHEMA_PATH`, `LOG_PATH`.
- Cria diretórios `data/` e `logs/` no import.
- Expõe `require_openrouter()` e `require_confluence()` — só validam quando o cliente real é instanciado, permitindo que `status`/`list` rodem sem credenciais.

### 4.2 `tools/parser.py`
- Único ponto de entrada: `parse(path) -> str`.
- Suporta `.pdf`, `.docx`, `.doc`. Extensões fora dessa lista → `UnsupportedDocumentError`.
- Texto vazio → `ValueError` (falha explícita, não retorna string vazia).
- Tabelas DOCX são linearizadas como `cell | cell | cell` para preservar estrutura tabular no prompt do LLM.

### 4.3 `tools/openrouter.py`
- Cliente HTTP único usando `httpx` síncrono.
- Timeout: 120s total, 15s connect.
- Suporte a `response_format={"type":"json_object"}` quando o caller pede JSON estrito.
- Headers `HTTP-Referer` e `X-Title` configurados para identificação no OpenRouter.
- Qualquer erro de transporte ou payload inesperado → `OpenRouterError` com contexto.

### 4.4 `tools/extractor.py`
- Schema Pydantic: `ExtractedDocument { product, type, rules[], technical_refs[], summary }`.
- `rule_id` validado contra regex `^RN-\d{1,4}$` (normalizado para uppercase).
- `type` restrito ao `Literal["business","technical"]`.
- `technical_refs.type` restrito a `table|endpoint|microservice|class`.
- Remove code-fences (` ``` `) antes de fazer `json.loads`.
- Falha de parse ou validação → `ExtractionError` com trecho do payload bruto para diagnóstico.

### 4.5 `tools/generator.py`
- Gera **Confluence Storage Format (XHTML)** — não markdown.
- Recebe `ExtractedDocument` + lista de `RelatedPageRef` para cross-links.
- Template hint embutido no prompt diferencia páginas `business` vs `technical`.
- Cross-references usam `<ac:link><ri:page ri:content-title="..."/></ac:link>`.
- `build_page_title(product, type)` é determinístico:
  - `business` → `"<Produto>"`
  - `technical` → `"<Produto> — Documentação Técnica"`
- Saída vazia → `GenerationError`.

### 4.6 `tools/confluence.py`
- Wrapper fino em torno de `atlassian-python-api` (modo cloud).
- Import de `atlassian` é **lazy** (dentro do `__init__`), permitindo que módulos importem `ConfluenceClient` sem a dependência instalada.
- `_ensure_parent_page(type_)` garante existência das páginas raiz:
  - `business` → `"Produtos & Regras de Negócio"`
  - `technical` → `"Técnico"`
- `upsert_page(title, body_xhtml, type_)`:
  - Lookup por título no space.
  - Update se existir, create se não — sob o parent correto.
- Constrói URLs no formato `<base>/wiki/spaces/<KEY>/pages/<id>`.

### 4.7 `graph/knowledge_graph.py`
- Bootstrap automático do schema na primeira conexão (`executescript(schema.sql)`).
- Context manager `transaction()` abre `sqlite3.Connection` com `foreign_keys=ON`, faz commit no sucesso e rollback em qualquer exceção.
- **Reads** retornam dataclasses `PageRecord` / `RuleRecord` imutáveis (`frozen=True`).
- **Writes** (`upsert_page`, `upsert_rule`, `link_rule_to_page`, `log_operation`) **exigem** uma `conn` aberta pelo caller — força o caller a controlar transação.
- `stats()` é o feed de `main.py status`.

### 4.8 `agent/orchestrator.py`
Único coordenador. Sequência de `_process(extracted, document_path)`:

1. Resolve `product`, `primary_type`, `opposite_type` (mapeamento `business↔technical`).
2. Consulta grafo: existe página primária? existe oposta?
3. Determina `operation` = `"update"` se primária existe, senão `"create"`.
4. Monta `RelatedPageRef` apontando para o título determinístico da página oposta (mesmo que ainda não exista no Confluence — facilita ao LLM gerar o cross-link).
5. Gera XHTML primário via `generator`.
6. `confluence.upsert_page(...)` da página primária.
7. **Cascata**: chama `_rules_changed(...)` que compara cada `rule_id` extraída contra o grafo. Se há regras novas *ou* `description` alterada **AND** a página oposta já existe → sintetiza `ExtractedDocument` para o tipo oposto (mergeando regras extraídas + regras conhecidas do grafo) e regenera + upserta a página oposta.
8. Abre `graph.transaction()` única e dentro dela:
   - `upsert_page` (primária + oposta se cascateou)
   - `upsert_rule` para cada regra extraída
   - `link_rule_to_page` ligando cada regra a cada página tocada
   - `log_operation` com `status="success"` e `detail` indicando se cascata ocorreu
9. Em qualquer exceção: `_log_failure(...)` grava entrada `status="error"` na tabela `operations` em transação separada — nunca mascara a exceção original.

> **Regra crítica do projeto:** alteração de regra implica avaliação obrigatória da página do tipo oposto. Implementada em `_rules_changed` + `_synthesize_opposite_doc`. Ambas as páginas são atualizadas no Confluence **antes** do commit da transação no grafo, mantendo o grafo consistente com o estado final do Confluence quando a transação fecha.

### 4.9 `main.py`
CLI baseado em `argparse`:

| Comando | Função |
|---------|--------|
| `ingest --file <path>` | Ingere um documento. |
| `ingest --folder <dir>` | Ingere todos os PDF/DOCX da pasta (continua mesmo se um falhar). |
| `status` | Imprime `KnowledgeGraph.stats()` em JSON (contadores + últimas 5 operações). |
| `list` | Lista produtos documentados e tipos de página por produto. |

---

## 5. Modelo de Dados (SQLite)

```sql
pages         (id, product, type, confluence_page_id, confluence_url, last_updated)
              UNIQUE(product, type)

rules         (id, rule_id, description, product, created_at)
              UNIQUE(rule_id)

dependencies  (id, rule_id → rules.rule_id, page_id → pages.id)
              UNIQUE(rule_id, page_id)

operations    (id, operation, document_path, pages_affected, rules_affected,
               status, detail, timestamp)
```

Índices em `pages(product,type)`, `rules(product)`, `dependencies(rule_id)`, `dependencies(page_id)`.

Campos JSON (`pages_affected`, `rules_affected`) são serializados via `json.dumps(list(...))` no orquestrador antes do insert.

---

## 6. Fluxo Completo (Sequence)

```
CLI (main.py)
  │  ingest --file foo.pdf
  ▼
Orchestrator.ingest_file(path)
  ├─ parser.parse(path)               → texto bruto
  ├─ extractor.extract(text)          → ExtractedDocument (LLM call)
  ├─ graph.get_page(product, primary) → existe? (read)
  ├─ graph.get_page(product, opposite) → existe? (read)
  ├─ generator.generate_page_xhtml(primary, related_refs)   → XHTML (LLM call)
  ├─ confluence.upsert_page(primary)  → ConfluencePage
  │
  ├─ se _rules_changed AND opposite existe:
  │   ├─ _synthesize_opposite_doc(...)                       → ExtractedDocument sintética
  │   ├─ generator.generate_page_xhtml(opposite, related)    → XHTML (LLM call)
  │   └─ confluence.upsert_page(opposite)                    → ConfluencePage
  │
  └─ graph.transaction():                                     ← uma única transação
      ├─ upsert_page(primary)
      ├─ upsert_page(opposite)  [se cascateou]
      ├─ upsert_rule(...) x N
      ├─ link_rule_to_page(...) x N×pages
      └─ log_operation(status='success')
```

Falha em qualquer etapa anterior à transação → `_log_failure` grava `status='error'` em transação separada e re-lança a exceção.

---

## 7. Tipos de Página

### Business
- Parent: `Produtos & Regras de Negócio`
- Título: `<Produto>`
- Seções: O que é, Para quem se aplica, Regras de Negócio (tabela ID/Descrição/Condição), Casos de Uso, Referências Técnicas (link).

### Technical
- Parent: `Técnico`
- Título: `<Produto> — Documentação Técnica`
- Seções: Visão Geral do Fluxo, APIs e Endpoints, Tabelas Populadas, Microserviços Envolvidos, Implementação de Regras de Negócio, Referências de Negócio (link).

Cross-link entre os dois tipos é obrigatório e renderizado via macro Confluence `<ac:link><ri:page .../></ac:link>`.

---

## 8. Configuração

`.env` (copiar de `.env.example`):

```
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=anthropic/claude-sonnet-4-5
CONFLUENCE_URL=https://<workspace>.atlassian.net
CONFLUENCE_USERNAME=...
CONFLUENCE_API_TOKEN=...
CONFLUENCE_SPACE_KEY=...
```

Variáveis opcionais com defaults em `config/settings.py`:
- `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`)
- `PARENT_BUSINESS_TITLE` (default `"Produtos & Regras de Negócio"`)
- `PARENT_TECHNICAL_TITLE` (default `"Técnico"`)

---

## 9. Tratamento de Erros

| Camada | Exceção tipada |
|--------|----------------|
| Parser | `FileNotFoundError`, `UnsupportedDocumentError`, `ValueError` (texto vazio) |
| OpenRouter | `OpenRouterError` (transporte, status ≥400, JSON malformado, shape inesperado) |
| Extractor | `ExtractionError` (LLM call falhou, JSON inválido, schema falha) |
| Generator | `GenerationError` (LLM call falhou, saída vazia) |
| Confluence | `ConfluenceError` (API falhou, parent inválido) |
| Orchestrator | `OrchestratorError` (entrada inválida no nível do agente) |

Nenhuma camada captura `Exception` genérica para "engolir" erros — o orchestrator captura apenas para gravar `operations.status='error'` e re-lança.

---

## 10. Limites Atuais e Extensões Futuras

- **Não há merge de extrações parciais**: cada documento gera uma extração completa; o grafo armazena a última versão. Para versionamento histórico, adicionar tabela `rules_history`.
- **Não há paralelismo**: ingestão de pasta é sequencial. LLM calls e Confluence API são síncronos.
- **Confluence cloud apenas** (`cloud=True` no client). Server/DC exigiria ajuste em `ConfluenceClient.__init__`.
- **Sem detecção de conflitos com edições manuais** (requisito #5: agente assume escrita exclusiva).
- **Sem retry policy** em chamadas LLM/Confluence — falha transitória aborta a ingestão. Adicionar `tenacity` se necessário.
