"""Global settings loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # python-dotenv is optional at import time
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-5")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# GitHub (fallback REST — opcional se `gh` CLI estiver disponível e autenticado)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Investigação de código
CALL_CHAIN_DEPTH_DEFAULT = int(os.getenv("CALL_CHAIN_DEPTH_DEFAULT", "2"))
MAX_FILES_PER_EXTRACTION = int(os.getenv("MAX_FILES_PER_EXTRACTION", "40"))

# Local paths
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DB_PATH = DATA_DIR / "graph.db"
SCHEMA_PATH = PROJECT_ROOT / "graph" / "schema.sql"
LOG_PATH = LOG_DIR / "operations.log"
REPO_CACHE_DIR = DATA_DIR / "repo_cache"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPO_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def require_openrouter() -> None:
    _required("OPENROUTER_API_KEY")
