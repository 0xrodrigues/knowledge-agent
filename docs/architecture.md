# Arquitetura — Knowledge Agent

## 1. Visão Geral

O Knowledge Agent é uma CLI Python que responde, para um engenheiro prestes a mexer em código, "que regras de negócio vivem aqui" — a partir de um PR (git local ou GitHub), de uma branch, ou de uma área/pacote inteiro do repositório. A fonte de verdade é o código real; documentação já commitada no repo é ponto de partida, nunca aceita cegamente.

```
origem (PR/branch/area) → Camada 0 (doc do repo) → Camada 1 (cache SQLite)
    → Camada 2 (código real, LLM) → merge + coverage → grafo (write+log) → resposta no terminal
```

O agente não publica em nenhum sistema externo. O SQLite local é cache de conhecimento entre execuções, consultado antes de qualquer leitura de código.

---

## 2. Princípios de Design

| Princípio | Como é garantido |
|-----------|------------------|
| **Código como fonte de verdade** | Regra vinda de documentação (`origin=repo_doc`) nunca recebe `status` diferente de `doc_only` até a Camada 2 rodar sobre os arquivos correspondentes. |
| **Cache antes de LLM** | `rule_sources.content_hash` guarda o hash do arquivo no momento da extração; se o hash atual bater, a regra é reaproveitada sem chamar o LLM. |
| **Atomicidade** | Todas as escritas no grafo de uma consulta ocorrem em uma única transação SQLite. Falha em qualquer etapa → rollback completo. |
| **Idempotência** | Constraints `UNIQUE` em `components(name)`, `rules(rule_id)`, `rule_sources(rule_id,file_path)`, `technical_refs(component_id,type,name)` + `ON CONFLICT DO UPDATE`. Reprocessar o mesmo PR/branch atualiza em vez de duplicar. |
| **Auditabilidade** | Toda operação (sucesso ou erro) é gravada na tabela `operations` e em `logs/operations.log`. |
| **Falha explícita** | Exceções tipadas por camada (`VcsError`, `GithubError`, `ExtractionError`, `OrchestratorError`). Nada é silenciado. |
| **Único coordenador** | `agent/orchestrator.py` é o único módulo que escreve no grafo. Demais módulos não tocam SQLite. |

---

## 3. Estrutura de Pastas

```
knowledge-agent/
├── config/
│   └── settings.py          # Carrega .env, expõe constantes + require_openrouter()
├── tools/
│   ├── docs_scanner.py      # Camada 0: README/docs/ADR/Javadoc já commitados no repo
│   ├── vcs.py                # Git local: diff, leitura de arquivo em ref, grep de símbolo
│   ├── github.py             # GitHub API: PR por número (gh CLI ou REST), sem clone
│   ├── code_scanner.py       # Camada 2: símbolos Java/Spring, expansão de call-chain, chunking
│   ├── extractor.py          # LLM: extract_from_docs / extract_from_code
│   └── openrouter.py         # Cliente HTTP genérico pro OpenRouter
├── graph/
│   ├── knowledge_graph.py    # Interface SQLite (reads públicos, writes via transaction)
│   └── schema.sql            # DDL das tabelas + índices
├── agent/
│   └── orchestrator.py       # Coordena as 3 camadas; único escritor do grafo
├── data/graph.db              # SQLite local (auto-gerado)
├── data/repo_cache/           # Clones automáticos (quando só --repo é passado, sem --repo-path)
├── logs/operations.log        # Auditoria estruturada
├── docs/architecture.md       # Este documento
├── tests/                     # pytest — fixture de repo git real (tmp_java_repo)
├── main.py                    # CLI: context / status / list
├── requirements.txt
└── .env.example
```

---

## 4. Camadas e Responsabilidades

### 4.1 `config/settings.py`
- Lê variáveis do `.env` (dotenv é opcional em tempo de import).
- Define caminhos absolutos: `DB_PATH`, `SCHEMA_PATH`, `LOG_PATH`, `REPO_CACHE_DIR` (destino dos clones automáticos).
- Expõe `CALL_CHAIN_DEPTH_DEFAULT` e `MAX_FILES_PER_EXTRACTION`, usados como default/cap na expansão de código.
- Expõe `require_openrouter()` — só valida quando o cliente real é instanciado, permitindo que `status`/`list` rodem sem credenciais.

### 4.2 `tools/vcs.py` — git local
- `diff_branches(repo_root, base, head) -> DiffResult`: `git diff --name-status` + `git diff --unified=3` por arquivo.
- `read_file_at_ref(repo_root, ref, path) -> str`: `git show <ref>:<path>` — lê conteúdo em qualquer commit sem checkout, preservando a working tree do engenheiro.
- `list_files_in_area(repo_root, area) -> list[str]`: `git ls-tree` para o modo `--area`.
- `grep_symbol(repo_root, symbol) -> list[GrepHit]`: `git grep -n -F`, usado pelo `code_scanner` para seguir cadeia de chamadas.
- `clone_or_update(url, dest) -> Path`: se `dest/.git` já existe, faz `git fetch --all --prune`; senão faz `git clone url dest`. Usado pelo orchestrator quando o engenheiro passa só `--repo` (sem `--repo-path`) — permite rodar `context --branch`/`--area`/`--pr --full-flow` a partir só do nome do repo, sem clone manual prévio.
- Qualquer falha de `git` → `VcsError` com stderr completo embutido.

### 4.3 `tools/github.py` — GitHub API
- `fetch_pr_metadata` / `fetch_pr_diff` / `read_file_at_ref`, todos aceitando `repo="owner/name"`.
- `normalize_repo(repo) -> str`: aceita `owner/name`, URL `https://github.com/...` ou remote SSH `git@github.com:...` e normaliza para `owner/name`. `clone_url_for(repo) -> str`: monta a URL HTTPS de clone a partir de qualquer uma dessas formas — usados pelo orchestrator para resolver `--repo` em `vcs.clone_or_update`.
- Prefere o `gh` CLI (já autenticado na maioria dos ambientes); cai para REST (`httpx` + `GITHUB_TOKEN`) quando `gh` não está instalado.
- `read_file_at_ref` via API é o que permite ao **modo PR** funcionar sem clonar o repositório (quando `--full-flow` não é usado).
- Qualquer falha → `GithubError` com o corpo da resposta/stderr embutido.

### 4.4 `tools/docs_scanner.py` — Camada 0
- `find_repo_docs(repo_root, area=None) -> list[DocHit]`: varre `README*.md`, `/docs/**/*.md`, `/docs/adr/**/*.md` (ou `/adr/**/*.md`), e blocos Javadoc (`/** ... */`) acima de classes/métodos.
- Puramente leitura de disco — sem LLM, sem git, sem rede.

### 4.5 `tools/code_scanner.py` — Camada 2, Java/Spring-aware
- `extract_symbols(file_path, content) -> list[JavaSymbol]`: regex sobre assinatura Java + anotações da linha anterior. Não é parsing real (sem `tree-sitter`/`javaparser`), mas cobre o padrão idiomático Spring Boot.
- `infer_technical_refs(symbols) -> list[TechnicalRef]`: mapeia anotação → tipo **sem LLM** — `@GetMapping`/`@PostMapping`/etc → `endpoint`; `@Entity`+`@Table` → `table`; `@Service`/`@Component`/`@Repository` → `class`.
- `expand_call_chain(repo_root, seed_files, max_depth, max_files) -> ExpandedScope`: usa `vcs.grep_symbol` para achar onde métodos dos `seed_files` são chamados no resto do repo, expandindo por `max_depth` níveis. Só funciona com repo local.
- `chunk_for_llm(files, max_chars) -> list[dict]`: agrupa arquivos em lotes que cabem no contexto do LLM; trunca explicitamente arquivos individuais grandes demais.

### 4.6 `tools/extractor.py` — LLM
- Schemas Pydantic: `ExtractedRule` (`rule_id?`, `description`, `condition`, `category`, `confidence`, `origin`, `status`, `source_files[]`, `evidence`), `TechnicalRef`, `CodeContext`.
- `extract_from_docs(component, hits) -> CodeContext`: roda sobre `DocHit` da Camada 0. **Força** `origin="repo_doc"` e `status="doc_only"` no retorno, independente do que o LLM tenha respondido — defesa em profundidade do princípio "doc nunca é verdade confirmada".
- `extract_from_code(*, component, files, mode, known_rules, doc_candidates) -> CodeContext`: roda sobre código real; recebe as regras já conhecidas e os candidatos de doc como contexto no prompt, e resolve `status` para `code_confirmed` / `code_contradicts_doc` / `code_only`.
- `rule_id` só é preenchido pelo LLM quando existe uma tag explícita (`RN-XX`) no texto/código; caso contrário fica `None` e a resolução acontece no orchestrator.
- Parsing do JSON de resposta é defensivo: `technical_ref` com `type` fora do enum é descartado (com log de warning), não derruba a extração inteira; barras invertidas inválidas (texto de doc colado cru pelo LLM, ex. `R\$`) são reparadas com regex antes do segundo `json.loads`. Falha em ambas → `ExtractionError` com o corpo bruto truncado.

### 4.7 `graph/knowledge_graph.py`
- Bootstrap automático do schema na primeira conexão (`executescript(schema.sql)`).
- Context manager `transaction()` abre `sqlite3.Connection` com `foreign_keys=ON`, faz commit no sucesso e rollback em qualquer exceção.
- **Reads** retornam dataclasses imutáveis (`ComponentRecord`, `RuleRecord`, `RuleSourceRecord`, `TechnicalRefRecord`).
- **Writes** (`upsert_component`, `upsert_rule`, `upsert_rule_source`, `upsert_technical_ref`, `log_operation`) **exigem** uma `conn` aberta pelo caller.
- `rules_touching_files(paths)` e `source_hash_for(rule_id, file_path)` são a base da Camada 1 (cache).
- `stats()` é o feed de `main.py status`.

### 4.8 `agent/orchestrator.py`
Único coordenador. Sequência de `Orchestrator.answer(request: ContextRequest)`:

1. **Resolve origem** — `_resolve_pr` / `_resolve_branch` / `_resolve_area`, cada uma devolvendo um `_ResolvedOrigin` (arquivos em escopo, função `read_file`, `repo_root` opcional). Quando o request só tem `repo` (sem `repo_path`), `_ensure_repo_path` clona/atualiza automaticamente em `data/repo_cache/<owner>__<name>` via `github.normalize_repo` + `vcs.clone_or_update` antes de seguir — modo `branch`/`area` sempre precisa disso; modo `pr_number` só precisa quando `full_flow=True` (senão lê tudo via API do GitHub, sem clone).
2. **Deriva componente** — usa `request.area` se fornecido, senão o prefixo de path comum entre os arquivos em escopo.
3. **Camada 0** — `docs_scanner.find_repo_docs` + `extract_from_docs`, só quando há `repo_root` local.
4. **Camada 1** — lê cada arquivo em escopo uma vez, calcula hash, compara contra `rule_sources.content_hash` de regras `status="code_confirmed"` já conhecidas. Arquivo com hash batendo não entra na Camada 2.
5. **Camada 2** — para os arquivos que faltam: se `full_flow=True`, `code_scanner.expand_call_chain` primeiro; depois `extract_from_code` em lotes (`chunk_for_llm`), passando as regras conhecidas e os candidatos de doc como contexto.
6. **Merge** — regras extraídas na Camada 2 sempre vencem sobre cache/doc para a mesma chave (`rule_id` ou descrição normalizada); regras de doc cujo arquivo nunca foi verificado nesta consulta permanecem `doc_only`; regras reaproveitadas do cache mantêm seu `status` armazenado.
7. **Coverage** — `"full"` quando `full_flow=True` sem truncamento (por profundidade ou limite de arquivos) ou quando o componente já tinha scan completo neste mesmo commit; `"partial"` no resto. `coverage_reason` é sempre uma string determinística, nunca gerada por LLM.
8. **Escreve** — `graph.transaction()` única: `upsert_component` (marcando `last_full_scan_commit` quando aplicável), `upsert_rule` + `upsert_rule_source` por regra (resolvendo `rule_id` faltante contra regras existentes por descrição normalizada, ou alocando `RN-AUTO-<n>`), `upsert_technical_ref`, `log_operation`.
9. Em qualquer exceção: `_log_failure` grava `status="error"` em transação separada — nunca mascara a exceção original.

Cada fase (`resolve_origin`, `docs_scan`, `code_scan`, `llm_extract`) também é logada individualmente via `_log_step`, como operação `<flow_context|pr_context>:<fase>` com status `in_progress`/`success`/`error` — dá visibilidade de progresso em `status`/`list` durante execuções longas, além do registro final da operação completa. Falha ao gravar um step nunca aborta a consulta (só loga exceção).

> **Regra crítica do projeto:** nenhuma regra de negócio é reportada como confirmada sem ter passado pela Camada 2 nesta consulta ou em uma consulta anterior cujo hash de arquivo ainda seja válido. `status="code_contradicts_doc"` é o sinal de maior valor do sistema — documentação desatualizada encontrada durante a análise do código real.

### 4.9 `main.py`
CLI baseado em `argparse`:

| Comando | Função |
|---------|--------|
| `context --pr <n> [--repo owner/name] [--repo-path .] [--full-flow] [--depth N] [--json]` | Contexto a partir de um PR (via API do GitHub por padrão; local + clone automático quando `--full-flow` ou `--repo-path` é usado). |
| `context --branch <b> --base <b> [--repo owner/name \| --repo-path .] [--full-flow] [--depth N] [--json]` | Contexto a partir de diff entre branches locais. `--repo` sem `--repo-path` clona/atualiza automaticamente em `data/repo_cache/`. |
| `context --area <path> [--repo owner/name \| --repo-path .] [--depth N] [--json]` | Contexto de uma área/pacote inteiro (sempre full-flow). `--repo` sem `--repo-path` clona/atualiza automaticamente. |
| `status` | Imprime `KnowledgeGraph.stats()` em JSON. |
| `list` | Lista componentes documentados e contagem de regras/refs. |

---

## 5. Modelo de Dados (SQLite)

```sql
components    (id, name, description, last_full_scan_commit, last_full_scan_at)
              UNIQUE(name)

rules         (id, rule_id, component_id → components.id, description, condition,
               category, confidence, origin, status, created_at, updated_at)
              UNIQUE(rule_id)

rule_sources  (id, rule_id → rules.rule_id, file_path, line_range, source_kind,
               content_hash, commit_sha, last_verified_at)
              UNIQUE(rule_id, file_path)

technical_refs(id, component_id → components.id, type, name, description,
               file_path, line_range, updated_at)
              UNIQUE(component_id, type, name)

operations    (id, operation, source_ref, components_affected, rules_affected,
               status, detail, timestamp)
```

`rule_sources.content_hash` + `commit_sha` é o mecanismo de staleness: hash divergente → regra reprocessada na próxima consulta em vez de reaproveitada cegamente.

---

## 6. Fluxo Completo (Sequence)

```
CLI (main.py)
  │  context --branch feature/x --base main --repo-path . --full-flow
  ▼
Orchestrator.answer(request)
  ├─ _resolve_branch → DiffResult (vcs.diff_branches)
  ├─ _derive_component
  ├─ docs_scanner.find_repo_docs → extract_from_docs        → doc_rules (Camada 0, LLM call)
  ├─ leitura de cada seed file + hash                        → files_needing_scan (Camada 1)
  ├─ code_scanner.expand_call_chain (se full_flow)            → related_files
  ├─ chunk_for_llm + extract_from_code por lote                → code_rules (Camada 2, LLM call)
  ├─ code_scanner.infer_technical_refs (arquivos .java)         → refs determinísticos, sem LLM
  ├─ merge (reused + code + doc) + coverage
  └─ graph.transaction():
      ├─ upsert_component (+ last_full_scan_commit se full_flow sem truncamento)
      ├─ upsert_rule + upsert_rule_source  x N regras
      ├─ upsert_technical_ref  x N refs
      └─ log_operation(status='success')
```

Falha em qualquer etapa anterior à transação final → `_log_failure` grava `status='error'` em transação separada e re-lança a exceção.

---

## 7. Configuração

`.env` (copiar de `.env.example`):

```
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=anthropic/claude-sonnet-4-5
GITHUB_TOKEN=...   # opcional — só necessário sem `gh` CLI instalado/autenticado
```

Variáveis opcionais com defaults em `config/settings.py`:
- `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`)
- `CALL_CHAIN_DEPTH_DEFAULT` (default `2`)
- `MAX_FILES_PER_EXTRACTION` (default `40`)

---

## 8. Tratamento de Erros

| Camada | Exceção tipada |
|--------|----------------|
| VCS local | `VcsError` (git falhou, ref/arquivo inexistente) |
| GitHub | `GithubError` (gh/API falhou, shape inesperado) |
| OpenRouter | `OpenRouterError` (transporte, status ≥400, JSON malformado) |
| Extractor | `ExtractionError` (LLM call falhou, JSON inválido, schema falha) |
| Orchestrator | `OrchestratorError` (requisição inválida, falha em qualquer etapa das 3 camadas) |

Nenhuma camada captura `Exception` genérica para "engolir" erros — o orchestrator captura apenas para gravar `operations.status='error'` e re-lança.

---

## 9. Limites Atuais e Extensões Futuras

- **Symbol extraction é heurística (regex), não parsing real** — cobre o padrão idiomático Spring Boot, mas pode perder símbolos em código muito fora do convencional. Evolução futura: `tree-sitter`/`javaparser`.
- **`expand_call_chain` só busca por nome de método via grep textual** — pode gerar falsos positivos em nomes de método muito genéricos, e não segue interfaces/polimorfismo.
- **Sem versionamento histórico de regra** — o grafo guarda a última extração conhecida; não há `rules_history`.
- **Sem paralelismo** — LLM calls e leitura de arquivos são síncronas.
- **Sem retry policy** em chamadas LLM/GitHub — falha transitória aborta a consulta.
