"""Knowledge Agent CLI entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.orchestrator import IngestResult, Orchestrator
from graph.knowledge_graph import KnowledgeGraph


def _print_ingest_result(r: IngestResult) -> None:
    print(f"  document : {r.document_path}")
    print(f"  product  : {r.product}")
    print(f"  type     : {r.primary_type}")
    print(f"  op       : {r.operation}")
    print(f"  rules    : {', '.join(r.rules_seen) or '(none)'}")
    for page in r.pages_touched:
        print(f"  page     : [{page.page_id}] {page.title} -> {page.url}")


def cmd_ingest(args: argparse.Namespace) -> int:
    orch = Orchestrator()
    if args.file:
        result = orch.ingest_file(args.file)
        print("Ingested:")
        _print_ingest_result(result)
        return 0
    if args.folder:
        results = orch.ingest_folder(args.folder)
        if not results:
            print("No documents ingested. Nothing to do.")
            return 0
        for r in results:
            print("Ingested:")
            _print_ingest_result(r)
            print("-" * 40)
        return 0
    print("Provide either --file or --folder.", file=sys.stderr)
    return 2


def cmd_status(_: argparse.Namespace) -> int:
    graph = KnowledgeGraph()
    stats = graph.stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    graph = KnowledgeGraph()
    products = graph.list_products()
    if not products:
        print("(no products documented yet)")
        return 0
    for product in products:
        pages = graph.list_pages_for_product(product)
        labels = ", ".join(f"{p.type}({p.confluence_page_id})" for p in pages)
        print(f"- {product}: {labels}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge-agent",
        description="Institutional knowledge agent: ingest docs, publish to Confluence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Ingest a document or folder of documents.")
    group = ing.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Path to a single PDF/DOCX file.")
    group.add_argument("--folder", type=Path, help="Folder containing documents.")
    ing.set_defaults(func=cmd_ingest)

    st = sub.add_parser("status", help="Show graph statistics.")
    st.set_defaults(func=cmd_status)

    ls = sub.add_parser("list", help="List documented products.")
    ls.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
