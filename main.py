"""Knowledge Agent CLI entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from agent.orchestrator import ContextRequest, Orchestrator, OrchestratorError
from config.settings import CALL_CHAIN_DEPTH_DEFAULT
from graph.knowledge_graph import KnowledgeGraph


def _print_context_answer(answer) -> None:
    print("=== Contexto de Regras de Negocio ===")
    print(f"Componente   : {answer.component}")
    print(f"Origem       : {answer.origin_description}")
    print(f"Cobertura    : {answer.coverage}")
    print(f"Motivo       : {answer.coverage_reason}")
    print()

    if not answer.rules:
        print("Regras: (nenhuma encontrada)")
    else:
        print("Regras:")
        for rule in answer.rules:
            print(
                f"  [{rule.rule_id or '???'}] {rule.category}  "
                f"confidence={rule.confidence}  status={rule.status}  origin={rule.origin}"
            )
            print(f"    {rule.description}")
            if rule.condition:
                print(f"    condicao : {rule.condition}")
            for src in rule.source_files:
                loc = f"{src.file}:{src.lines}" if src.lines else src.file
                print(f"    arquivo  : {loc}")
            if rule.evidence:
                print(f"    evidencia: {rule.evidence}")
            print()

    if answer.technical_refs:
        print("Referencias tecnicas:")
        for ref in answer.technical_refs:
            loc = ref.source_files[0].file if ref.source_files else ""
            print(f"  [{ref.type}] {ref.name}  -> {loc}")
        print()

    print(f"Resumo: {answer.summary}")


def cmd_context(args: argparse.Namespace) -> int:
    if args.pr is not None:
        mode = "pr_number"
    elif args.branch is not None:
        mode = "branch"
    else:
        mode = "area"

    full_flow = args.full_flow or (mode == "area")

    request = ContextRequest(
        mode=mode,
        pr_number=args.pr,
        branch=args.branch,
        base_branch=args.base,
        area=args.area,
        repo_path=args.repo_path,
        repo=args.repo,
        full_flow=full_flow,
        call_chain_depth=args.depth,
    )

    try:
        orch = Orchestrator()
        answer = orch.answer(request)
    except OrchestratorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "component": answer.component,
                    "origin_description": answer.origin_description,
                    "coverage": answer.coverage,
                    "coverage_reason": answer.coverage_reason,
                    "rules": [r.model_dump() for r in answer.rules],
                    "technical_refs": [t.model_dump() for t in answer.technical_refs],
                    "summary": answer.summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_context_answer(answer)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    graph = KnowledgeGraph()
    stats = graph.stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    graph = KnowledgeGraph()
    components = graph.list_components()
    if not components:
        print("(nenhum componente documentado ainda)")
        return 0
    for component in components:
        rules = graph.rules_for_component(component.name)
        refs = graph.technical_refs_for_component(component.name)
        print(f"- {component.name}: {len(rules)} regra(s), {len(refs)} referencia(s) tecnica(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge-agent",
        description="Agente de contexto de codigo: extrai regras de negocio implementadas.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ctx = sub.add_parser(
        "context", help="Extrai contexto de regras de negocio de um PR, branch ou area."
    )
    origin_group = ctx.add_mutually_exclusive_group(required=True)
    origin_group.add_argument("--pr", type=int, help="Numero do PR no GitHub.")
    origin_group.add_argument("--branch", type=str, help="Branch local a comparar com --base.")
    origin_group.add_argument("--area", type=str, help="Caminho/pacote a analisar por completo.")
    ctx.add_argument("--base", type=str, default="main", help="Branch base para --branch (default: main).")
    ctx.add_argument(
        "--repo",
        type=str,
        help="Repositorio: 'owner/name' ou URL do GitHub. Sem --repo-path, o agente clona "
        "automaticamente em cache local (data/repo_cache/).",
    )
    ctx.add_argument(
        "--repo-path",
        type=Path,
        help="Caminho de um clone local ja existente (opcional — evita o clone automatico).",
    )
    ctx.add_argument(
        "--full-flow",
        action="store_true",
        help="Expande a analise pela cadeia de chamadas, alem do diff.",
    )
    ctx.add_argument(
        "--depth", type=int, default=CALL_CHAIN_DEPTH_DEFAULT, help="Profundidade da expansao de call-chain."
    )
    ctx.add_argument("--json", action="store_true", help="Imprime a resposta em JSON.")
    ctx.set_defaults(func=cmd_context)

    st = sub.add_parser("status", help="Mostra estatisticas do grafo.")
    st.set_defaults(func=cmd_status)

    ls = sub.add_parser("list", help="Lista componentes documentados.")
    ls.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
