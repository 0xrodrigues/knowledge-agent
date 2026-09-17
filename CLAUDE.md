# CLAUDE.md — Knowledge Agent

## O que é esse projeto

Agente de contexto de código para engenheiros. Antes de mexer numa funcionalidade, o engenheiro pergunta ao agente "que regras de negócio vivem aqui" — a partir de um PR (git local ou GitHub), de uma branch, ou de uma área/pacote inteiro do repositório.

**Princípio central:** a maior fonte de verdade sobre regras de negócio é o que está implementado no código — não o que documentação de produto/negócio "acha" que é. Documentação já commitada no repo (README, `/docs`, ADRs, Javadoc) é ponto de partida barato, mas nunca é aceita como verdade sem confronto com o código real quando o código está em escopo da consulta.

O agente responde direto no terminal do engenheiro. Não publica em nenhum sistema externo. O SQLite local é cache de conhecimento entre execuções, não um sistema de publicação.

---

## Stack

- **Python 3.11+**
- **LLM:** OpenRouter via `httpx` — modelo padrão: `anthropic/claude-sonnet-4-5`
- **VCS:** `git` (subprocess) para repositório local; `gh` CLI ou REST (`httpx` + `GITHUB_TOKEN`) para GitHub
- **Grafo local:** SQLite (`sqlite3` built-in)
- **Validação:** `pydantic`
- **Env vars:** `python-dotenv`
- **Código-alvo investigado:** majoritariamente Java/Spring Boot

---

## Estrutura do Projeto

```
knowledge-agent/
├── config/
│   └── settings.py              # Env vars e constantes globais
├── tools/
│   ├── docs_scanner.py          # Camada 0: README/docs/ADR/Javadoc já commitados
│   ├── vcs.py                   # Git local: diff, leitura de arquivo em ref, grep de símbolo
│   ├── github.py                # GitHub API: PR por número, sem precisar de clone
│   ├── code_scanner.py          # Camada 2: símbolos Java/Spring, expansão de call-chain
│   ├── extractor.py             # LLM: extrai regras de doc e de código
│   └── openrouter.py            # Cliente HTTP genérico pro OpenRouter
├── graph/
│   ├── knowledge_graph.py       # Interface pública do grafo (leitura)
│   └── schema.sql               # Definição das tabelas SQLite
├── agent/
│   └── orchestrator.py          # Coordena as 3 camadas; único que escreve no grafo
├── data/
│   └── graph.db                 # SQLite (gerado automaticamente)
├── logs/
│   └── operations.log
├── tests/                       # pytest — fixture de repo git real, sem custo de LLM/rede
├── .env.example
├── requirements.txt
└── main.py                      # Entrypoint CLI (`context`, `status`, `list`)
```

---

## Regras de Arquitetura

### Separação de responsabilidades
- **tools/** — funções puras, sem estado, sem conhecimento do fluxo
- **graph/** — camada de dados compartilhada; tools apenas leem
- **agent/orchestrator.py** — único componente que escreve no grafo e coordena o fluxo

### Nunca violar
1. Só o `orchestrator.py` escreve no grafo (INSERT/UPDATE). Tools apenas fazem SELECT.
2. Toda regra `category=business` extraída deve, quando o código permitir, vir amarrada a suas `technical_refs` correspondentes na mesma operação — nunca devolver regra sem apontar arquivo-fonte.
3. Toda consulta de contexto segue a ordem **doc do repo → cache local → código-fonte**. A camada de código só roda para arquivos que não foram confirmados ainda ou cujo `content_hash` divergiu do armazenado. Documentação do repo nunca é tratada como verdade final sem confronto com código quando o código está em escopo da consulta.
4. Toda operação é registrada na tabela `operations` com status e detalhes, independentemente de sucesso ou falha.
5. Erros devem ser verbosos e explícitos. Nunca silenciar exceções.

---

## Proveniência e status de uma regra

Cada regra carrega `origin` (`repo_doc` | `explicit_tag` | `inferred`) e `status` (`doc_only` | `code_confirmed` | `code_contradicts_doc` | `code_only`). `status="code_contradicts_doc"` é o sinal de maior valor do sistema: documentação desatualizada em relação ao código real.

---

## Convenções de Código

- Type hints obrigatórios em todas as funções
- Pydantic para todos os schemas de entrada/saída de LLM
- Funções de tool devem ser puras: recebem input, retornam output, sem side effects
- Logs via `logging` padrão do Python, não `print()`
- Variáveis de ambiente nunca hardcoded — sempre via `config/settings.py`

---

## Chamadas LLM

Todas as chamadas ao OpenRouter passam por `httpx` diretamente (sem SDK), via `tools/openrouter.py`.

- `tools/extractor.py` sempre retorna JSON — validar com Pydantic antes de usar
- Temperature 0.1 para extração (determinística o quanto possível)
- Sempre incluir timeout nas chamadas

---

## CLI

```bash
python main.py context --pr 482 [--repo owner/name] [--repo-path .] [--full-flow] [--depth N] [--json]
python main.py context --branch feature/x --base main --repo-path . [--full-flow] [--depth N] [--json]
python main.py context --area services/payments --repo-path . [--depth N] [--json]
python main.py status
python main.py list
```

---

## Testes

`pytest` a partir de `.venv`. `tests/conftest.py` cria um repositório git real e efêmero (`tmp_java_repo`) com um projeto Spring Boot mínimo — os testes de `vcs`/`docs_scanner`/`code_scanner` rodam contra ele de verdade, sem tocar repositório real da Cielo. `tools/extractor.py` e `tools/github.py` são sempre monkeypatchados/mockados nos testes — nenhuma suíte automatizada chama a API real do OpenRouter ou do GitHub.

---

## O que não fazer

- Não adicionar frameworks web (FastAPI, Flask) nessa fase — o agente é CLI only
- Não criar lógica de negócio dentro das tools — isso é responsabilidade do orchestrator
- Não fazer chamadas ao GitHub fora de `tools/github.py`, nem ao git local fora de `tools/vcs.py`
- Não fazer chamadas ao OpenRouter fora de `tools/extractor.py`
- Não aceitar regra vinda de documentação como confirmada sem passar pela camada de código quando o código estiver em escopo
