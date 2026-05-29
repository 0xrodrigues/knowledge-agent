# CLAUDE.md — Knowledge Agent

## O que é esse projeto

Agente de conhecimento institucional que ingere documentos internos (PDF/Word), extrai regras e entidades via LLM, mantém um grafo de dependências local (SQLite) e publica/atualiza páginas estruturadas no Confluence.

**Princípio central:** o agente é o único escritor do Confluence. Nenhuma edição humana ocorre no space. O grafo local é a fonte de verdade sobre o que existe e como está conectado.

---

## Stack

- **Python 3.11+**
- **LLM:** OpenRouter via `httpx` — modelo padrão: `anthropic/claude-sonnet-4-5`
- **Confluence:** `atlassian-python-api`
- **Parse:** `pymupdf` (PDF), `python-docx` (Word)
- **Grafo local:** SQLite (`sqlite3` built-in)
- **Validação:** `pydantic`
- **Env vars:** `python-dotenv`

---

## Estrutura do Projeto

```
knowledge-agent/
├── config/
│   └── settings.py              # Env vars e constantes globais
├── tools/
│   ├── parser.py                # PDF/Word → texto bruto
│   ├── extractor.py             # LLM: classifica, extrai regras e entidades
│   ├── generator.py             # LLM: gera conteúdo Confluence (XHTML)
│   └── confluence.py            # Confluence API: criar/atualizar páginas
├── graph/
│   ├── knowledge_graph.py       # Interface pública do grafo (leitura)
│   └── schema.sql               # Definição das tabelas SQLite
├── agent/
│   └── orchestrator.py          # Coordena fluxo; único que escreve no grafo
├── data/
│   └── graph.db                 # SQLite (gerado automaticamente)
├── logs/
│   └── operations.log
├── .env.example
├── requirements.txt
└── main.py                      # Entrypoint CLI
```

---

## Regras de Arquitetura

### Separação de responsabilidades
- **tools/** — funções puras, sem estado, sem conhecimento do fluxo
- **graph/** — camada de dados compartilhada; tools apenas leem
- **agent/orchestrator.py** — único componente que escreve no grafo e coordena o fluxo

### Nunca violar
1. Só o `orchestrator.py` escreve no grafo (INSERT/UPDATE). Tools apenas fazem SELECT.
2. Atualização de uma regra RN-XX sempre processa as páginas `business` e `technical` do produto na mesma operação — nunca apenas um lado.
3. Toda operação é registrada na tabela `operations` com status e detalhes, independentemente de sucesso ou falha.
4. Erros devem ser verbosos e explícitos. Nunca silenciar exceções.

---

## Tipos de Documento

| Tipo | Conteúdo |
|------|----------|
| `business` | Definição do produto, requisitos funcionais, regras de negócio (RN-XX) |
| `technical` | Endpoints, tabelas, microserviços, fluxos de sistema |

O extractor classifica automaticamente. Um documento é sempre de um tipo só.

---

## Convenções de Código

- Type hints obrigatórios em todas as funções
- Pydantic para todos os schemas de entrada/saída de LLM
- Funções de tool devem ser puras: recebem input, retornam output, sem side effects
- Logs via `logging` padrão do Python, não `print()`
- Variáveis de ambiente nunca hardcoded — sempre via `config/settings.py`

---

## Chamadas LLM

Todas as chamadas ao OpenRouter passam por `httpx` diretamente (sem SDK).

```python
# Padrão de chamada
response = httpx.post(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
    json={
        "model": settings.OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1  # baixo para extração estruturada
    }
)
```

- Extractor e generator sempre retornam JSON — validar com Pydantic antes de usar
- Temperature 0.1 para extração, 0.3 para geração de conteúdo
- Sempre incluir timeout nas chamadas (`timeout=60.0`)

---

## Confluence

- Páginas vivem sob duas seções fixas: `Produtos & Regras de Negócio` e `Técnico`
- Páginas do mesmo produto são linkadas entre si via Confluence Storage Format
- Criar página: verificar se já existe no grafo antes de chamar a API
- Atualizar página: sempre incrementar `version.number` da página atual

---

## CLI

```bash
python main.py ingest --file ./docs/arquivo.pdf     # ingere um documento
python main.py ingest --folder ./docs/              # ingere pasta inteira
python main.py status                               # estado do grafo
python main.py list                                 # produtos documentados
```

---

## O que não fazer

- Não adicionar frameworks web (FastAPI, Flask) nessa fase — o agente é CLI only
- Não criar lógica de negócio dentro das tools — isso é responsabilidade do orchestrator
- Não fazer chamadas à Confluence API fora de `tools/confluence.py`
- Não fazer chamadas ao OpenRouter fora de `tools/extractor.py` e `tools/generator.py`
- Não usar `print()` para logging