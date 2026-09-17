# Knowledge Agent

Agente de contexto de código para engenheiros. Antes de mexer numa funcionalidade, pergunte ao agente "que regras de negócio vivem aqui" — a partir de um PR, de uma branch, ou de uma área/pacote inteiro do repositório.

**Princípio central:** a maior fonte de verdade sobre regras de negócio é o que está implementado no código — não o que a documentação de produto "acha" que é. Documentação já commitada no repo (README, `/docs`, ADRs, Javadoc) é ponto de partida barato, mas nunca é aceita como verdade sem confronto com o código real.

O agente não publica em nenhum sistema externo. Cada consulta gera um documento Markdown local (`reports/<componente>-<timestamp>.md`) com a investigação completa.

---

## Quickstart

```bash
git clone <url-deste-repo>
cd knowledge-agent

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # inclui requirements.txt + pytest/respx

cp .env.example .env
# preenche OPENROUTER_API_KEY no .env
```

`GITHUB_TOKEN` só é necessário se você não tiver o `gh` CLI instalado/autenticado — o agente prefere `gh` quando disponível.

---

## Uso

Três formas de apontar a origem — sempre uma delas, nunca mais de uma:

```bash
# PR do GitHub, sem precisar clonar (via API)
.venv/bin/python main.py context --pr 482 --repo owner/name

# PR + fluxo completo (precisa de clone; automático se só --repo for passado)
.venv/bin/python main.py context --pr 482 --repo owner/name --full-flow

# Diff entre duas branches locais
.venv/bin/python main.py context --branch feature/x --base main --repo-path .

# Área/pacote inteiro do repositório (sempre full-flow)
.venv/bin/python main.py context --area src/main/java/com/empresa/pagamentos --repo-path .
```

Se você passar só `--repo` (URL do GitHub ou `owner/name`), sem `--repo-path`, o agente clona/atualiza sozinho em `data/repo_cache/` — não precisa rodar `git clone` na mão:

```bash
.venv/bin/python main.py context --area . --repo=https://github.com/owner/name
```

Outros comandos:

```bash
.venv/bin/python main.py status   # estatisticas do grafo local (componentes, regras, operacoes)
.venv/bin/python main.py list     # componentes ja documentados e contagem de regras/refs
```

### Flags úteis

| Flag | Efeito |
|---|---|
| `--full-flow` | Expande a análise pela cadeia de chamadas, além do diff (precisa de `--repo-path` ou `--repo`). |
| `--depth N` | Profundidade da expansão de call-chain (default `2`). |
| `--json` | Imprime a resposta em JSON no stdout, em vez de escrever o documento Markdown — para consumo por outra ferramenta. |

---

## Output

Por padrão, `context` escreve um documento Markdown em `reports/<componente>-<timestamp>.md` e só imprime o caminho do arquivo no terminal:

```
Documento salvo em: reports/src-main-java-com-empresa-pagamentos-20260117-143022.md
```

O documento traz, por regra: `rule_id`, categoria, confiança, **status** (`doc_only` | `code_confirmed` | `code_contradicts_doc` | `code_only`), condição, arquivo(s)-fonte e evidência citada do código. `status="code_contradicts_doc"` é o sinal de maior valor — significa que a documentação encontrada diverge do que o código realmente faz.

---

## Como funciona (resumo)

Toda consulta passa por 3 camadas, sempre nessa ordem:

1. **Camada 0 — doc do repo.** README, `/docs`, ADRs, Javadoc já commitados. Candidatos baratos, nunca aceitos como verdade.
2. **Camada 1 — cache local (SQLite).** Compara o hash do conteúdo atual do arquivo contra o que já foi confirmado antes. Bate → reaproveita sem chamar LLM.
3. **Camada 2 — código real (LLM).** Só roda para o que a Camada 1 não conseguiu confirmar. Resolve cada regra como confirmada, contraditória em relação à doc, ou nova. Os lotes rodam em paralelo (`LLM_MAX_CONCURRENCY`, default 5).

Detalhes de arquitetura, schema do grafo e limites conhecidos: [`docs/architecture.md`](docs/architecture.md).

---

## Configuração (`.env`)

| Variável | Obrigatória | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Sim | — |
| `OPENROUTER_MODEL` | Não | `anthropic/claude-sonnet-4-5` |
| `GITHUB_TOKEN` | Não | — (só se não tiver `gh` CLI) |
| `LLM_MAX_CONCURRENCY` | Não | `5` |
| `CALL_CHAIN_DEPTH_DEFAULT` | Não | `2` |
| `MAX_FILES_PER_EXTRACTION` | Não | `40` |

---

## Desenvolvimento

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Nenhuma suíte automatizada chama a API real do OpenRouter ou do GitHub — `tools/extractor.py` e `tools/github.py` são sempre mockados nos testes. `tests/conftest.py` cria um repositório git real e efêmero (`tmp_java_repo`) para os testes de `vcs`/`docs_scanner`/`code_scanner`.

---

## Estrutura do projeto

```
knowledge-agent/
├── config/settings.py     # Env vars e constantes globais
├── tools/                 # Funções puras: docs_scanner, vcs, github, code_scanner, extractor, openrouter
├── graph/                 # Grafo local SQLite (schema + interface de leitura)
├── agent/orchestrator.py  # Coordena as 3 camadas; único que escreve no grafo
├── main.py                # CLI: context / status / list
├── tests/                 # pytest
├── data/                  # graph.db + repo_cache/ (gerado, git-ignorado)
└── reports/                # Documentos Markdown gerados por consulta (git-ignorado)
```
