# Knowledge Agent — Prompt de Desenvolvimento

## Contexto

Construir um agente de conhecimento institucional em Python. O agente ingere documentos (PDF/Word), extrai regras e entidades via LLM, mantém um grafo de dependências local e publica/atualiza páginas estruturadas no Confluence.

**Nenhuma atualização humana ocorre no Confluence.** O agente é a única fonte de escrita.

---

## Stack

- **Linguagem:** Python 3.11+
- **LLM:** OpenRouter (HTTP via `httpx`) — modelo padrão: `anthropic/claude-sonnet-4-5`
- **Confluence:** `atlassian-python-api`
- **Parse de documentos:** `pymupdf` (PDF), `python-docx` (Word)
- **Grafo local:** SQLite (`sqlite3` built-in)
- **Validação de schemas:** `pydantic`
- **Env vars:** `python-dotenv`

---

## Estrutura de Pastas

```
knowledge-agent/
├── config/
│   └── settings.py              # Carrega env vars, expõe constantes globais
│
├── tools/
│   ├── parser.py                # PDF/Word → texto bruto
│   ├── extractor.py             # LLM: extrai regras, entidades, tipo do documento
│   ├── generator.py             # LLM: gera conteúdo estruturado para Confluence
│   └── confluence.py            # Confluence API: criar/atualizar páginas
│
├── graph/
│   ├── knowledge_graph.py       # Interface pública do grafo (consultas e leitura)
│   └── schema.sql               # Definição das tabelas SQLite
│
├── agent/
│   └── orchestrator.py          # Único coordenador do fluxo; único que escreve no grafo
│
├── data/
│   └── graph.db                 # SQLite local (gerado automaticamente)
│
├── logs/
│   └── operations.log           # Auditoria de todas as operações do agente
│
├── .env.example                 # Template de variáveis de ambiente
├── requirements.txt
└── main.py                      # Entrypoint CLI
```

---

## Variáveis de Ambiente (`.env`)

```
OPENROUTER_API_KEY=
OPENROUTER_MODEL=anthropic/claude-sonnet-4-5

CONFLUENCE_URL=
CONFLUENCE_USERNAME=
CONFLUENCE_API_TOKEN=
CONFLUENCE_SPACE_KEY=
```

---

## Tipos de Documento

O agente reconhece dois tipos:

| Tipo | Conteúdo |
|------|----------|
| `business` | O que é o produto, requisitos funcionais, regras de negócio (RN-XX) |
| `technical` | Endpoints, classes, tabelas, microserviços, fluxos de sistema |

Um documento é sempre de um tipo só. O agente classifica automaticamente.

---

## Estrutura do Confluence

```
Space (CONFLUENCE_SPACE_KEY)
├── Produtos & Regras de Negócio
│   ├── [Nome do Produto]        ← página tipo `business`
│   └── ...
└── Técnico
    ├── [Nome do Produto]        ← página tipo `technical`
    └── ...
```

Páginas de negócio e técnica do mesmo produto são **linkadas entre si**.

---

## Schema do Grafo (SQLite)

```sql
-- Páginas criadas no Confluence
CREATE TABLE pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('business', 'technical')),
    confluence_page_id TEXT NOT NULL,
    confluence_url TEXT NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Regras de negócio extraídas
CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL UNIQUE,  -- ex: RN-06
    description TEXT NOT NULL,
    product TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Dependências entre regras e páginas
CREATE TABLE dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    page_id INTEGER NOT NULL,
    FOREIGN KEY (rule_id) REFERENCES rules(rule_id),
    FOREIGN KEY (page_id) REFERENCES pages(id)
);

-- Log de operações do agente
CREATE TABLE operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,       -- 'create', 'update', 'skip'
    document_path TEXT,
    pages_affected TEXT,           -- JSON array de page IDs
    rules_affected TEXT,           -- JSON array de rule IDs
    status TEXT NOT NULL,          -- 'success', 'error'
    detail TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Fluxo do Agente (Orchestrator)

```
main.py recebe caminho do documento
    → parser.py extrai texto bruto
    → extractor.py (LLM) classifica tipo, extrai produto, regras (RN-XX), referências técnicas
    → knowledge_graph.py consulta: produto já existe? quais regras estão linkadas?
    → generator.py (LLM) gera conteúdo estruturado para a página
    → confluence.py cria ou atualiza página no Confluence
    → se atualização: verifica páginas dependentes via grafo e processa todas na mesma operação
    → orchestrator escreve no grafo (pages, rules, dependencies, operations)
    → loga operação completa
```

**Regra crítica:** Se uma regra RN-XX é atualizada, o agente **obrigatoriamente** avalia e atualiza a página do outro tipo (business ↔ technical) na mesma transação. Nunca atualiza apenas um lado.

---

## Templates de Página

### Página de Negócio (`business`)

```
# [Nome do Produto]

## O que é
[Definição em 2-3 linhas]

## Para quem se aplica
[Elegibilidade]

## Regras de Negócio
| ID | Descrição | Condição |
|----|-----------|----------|
| RN-01 | ... | ... |

## Casos de Uso
[Exemplos práticos]

## Referências Técnicas
- [Ver documentação técnica → Técnico/[Nome do Produto]]

---
*Última atualização: [timestamp] | Fonte: [nome do arquivo original]*
```

### Página Técnica (`technical`)

```
# [Nome do Produto] — Documentação Técnica

## Visão Geral do Fluxo
[Descrição do fluxo de sistema]

## APIs e Endpoints
| Endpoint | Método | Descrição |
|----------|--------|-----------|
| /api/... | POST | ... |

## Tabelas Populadas
| Tabela | Coluna | Descrição |
|--------|--------|-----------|
| REPASS_RATE | CD_EXCECAO_FLAG | ... |

## Microserviços Envolvidos
[Lista de serviços]

## Implementação de Regras de Negócio
| Regra | Implementação Técnica |
|-------|-----------------------|
| RN-06 | Consultar CD_EXCECAO_FLAG em REPASS_RATE |

## Referências de Negócio
- [Ver regras de negócio → Produtos & Regras de Negócio/[Nome do Produto]]

---
*Última atualização: [timestamp] | Fonte: [nome do arquivo original]*
```

---

## Prompts LLM

### Extractor — Classificação e Extração

```
Você é um extrator de conhecimento institucional.

Analise o documento abaixo e retorne APENAS um JSON válido, sem markdown, sem explicações.

Schema esperado:
{
  "product": "nome do produto principal do documento",
  "type": "business" | "technical",
  "rules": [
    {
      "rule_id": "RN-XX",
      "description": "descrição clara da regra",
      "condition": "condição ou critério da regra"
    }
  ],
  "technical_refs": [
    {
      "type": "table" | "endpoint" | "microservice" | "class",
      "name": "nome",
      "description": "para que serve"
    }
  ],
  "summary": "resumo do documento em 2-3 linhas"
}

Documento:
{texto_bruto}
```

### Generator — Criação de Conteúdo

```
Você é um redator técnico especializado em documentação institucional.

Gere o conteúdo de uma página Confluence no formato Confluence Storage Format (XHTML).

Tipo de página: {type}
Produto: {product}
Dados extraídos: {extracted_json}
Páginas relacionadas: {related_pages}

Regras:
- Use os templates definidos para cada tipo de página
- Referências cruzadas devem usar o formato: <ac:link><ri:page ri:content-title="[título]"/></ac:link>
- Todas as regras RN-XX devem aparecer na tabela de regras
- Seja preciso, sem enrolação
- Retorne APENAS o conteúdo XHTML, sem explicações
```

---

## Entrypoint CLI (`main.py`)

```bash
# Ingerir um documento
python main.py ingest --file ./docs/parcelado-cliente-negocio.pdf

# Ingerir todos os documentos de uma pasta
python main.py ingest --folder ./docs/

# Verificar status do grafo
python main.py status

# Listar produtos documentados
python main.py list
```

---

## Requisitos de Implementação

1. **Atomicidade:** atualização de páginas dependentes ocorre na mesma operação ou não ocorre nenhuma
2. **Idempotência:** rodar o mesmo documento duas vezes não cria duplicatas — detecta e atualiza
3. **Auditabilidade:** toda operação é logada em `operations` com status e detalhes
4. **Falha explícita:** erros devem ser verbosos e específicos — nunca silenciosos
5. **Sem escrita humana:** o agente assume que é o único escritor do space; não valida conflitos com edições manuais

---

## Dependências (`requirements.txt`)

```
httpx
pymupdf
python-docx
atlassian-python-api
pydantic
python-dotenv
```