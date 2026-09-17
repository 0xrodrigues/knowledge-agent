"""Local git access: diffs, file-at-ref reads, symbol grep. No network calls."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger("knowledge_agent.vcs")

FileStatus = Literal["added", "modified", "deleted", "renamed"]


class VcsError(RuntimeError):
    """Raised when a git invocation fails or returns something unusable."""


@dataclass(frozen=True)
class FileChange:
    path: str
    status: FileStatus
    patch: str


@dataclass(frozen=True)
class DiffResult:
    source: Literal["local_git", "github_pr"]
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    files: list[FileChange]
    repo_root: Path | None


@dataclass(frozen=True)
class GrepHit:
    file: str
    line_number: int
    line_text: str


_STATUS_MAP: dict[str, FileStatus] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "added",
}


def _run_git(repo_root: Path, args: list[str]) -> str:
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VcsError(
            f"Failed to run 'git {' '.join(args)}' in {repo_root}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise VcsError(
            f"'git {' '.join(args)}' in {repo_root} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    logger.debug(
        "git %s em %s levou %.2fs", " ".join(args), repo_root, time.monotonic() - start
    )
    return result.stdout


def resolve_sha(repo_root: Path, ref: str) -> str:
    return _run_git(repo_root, ["rev-parse", ref]).strip()


def clone_or_update(url: str, dest: Path) -> Path:
    """Ensure `dest` is a local clone of `url`, cloning it if missing or
    fetching updates if it already exists. Lets the agent reach a repo given
    only a URL, without the caller having to run `git clone` by hand."""
    dest = Path(dest)
    start = time.monotonic()
    if (dest / ".git").is_dir():
        logger.info("Atualizando clone existente em %s (git fetch --all --prune)", dest)
        try:
            subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                cwd=dest,
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VcsError(f"Failed to update clone at {dest}: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            raise VcsError(
                f"'git fetch' in {dest} failed: {exc.stderr.strip()}"
            ) from exc
        logger.info(
            "Clone atualizado em %.1fs: %s", time.monotonic() - start, dest
        )
        return dest

    logger.info("Clonando %s -> %s (pode levar ate alguns minutos)", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VcsError(f"Failed to clone {url} into {dest}: {exc}") from exc
    if result.returncode != 0:
        raise VcsError(
            f"'git clone {url}' into {dest} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    logger.info("Clone concluido em %.1fs: %s", time.monotonic() - start, dest)
    return dest


def diff_branches(repo_root: Path, base: str, head: str) -> DiffResult:
    repo_root = Path(repo_root)
    logger.info("Calculando diff %s...%s em %s", base, head, repo_root)
    start = time.monotonic()
    base_sha = resolve_sha(repo_root, base)
    head_sha = resolve_sha(repo_root, head)

    name_status_out = _run_git(
        repo_root, ["diff", "--name-status", f"{base}...{head}"]
    )

    files: list[FileChange] = []
    for line in name_status_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0]
        status = _STATUS_MAP.get(code[0], "modified")
        path = parts[-1]  # renamed/copied entries carry old path before new path

        patch = _run_git(
            repo_root,
            ["diff", "--unified=3", f"{base}...{head}", "--", path],
        )
        files.append(FileChange(path=path, status=status, patch=patch))

    logger.info(
        "Diff calculado em %.2fs: %d arquivo(s) alterado(s) entre %s e %s",
        time.monotonic() - start,
        len(files),
        base,
        head,
    )
    return DiffResult(
        source="local_git",
        base_ref=base,
        head_ref=head,
        base_sha=base_sha,
        head_sha=head_sha,
        files=files,
        repo_root=repo_root,
    )


def read_file_at_ref(repo_root: Path, ref: str, path: str) -> str:
    repo_root = Path(repo_root)
    try:
        return _run_git(repo_root, ["show", f"{ref}:{path}"])
    except VcsError as exc:
        raise VcsError(
            f"Could not read '{path}' at ref '{ref}' in {repo_root}: {exc}"
        ) from exc


def list_files_in_area(repo_root: Path, area: str, ref: str = "HEAD") -> list[str]:
    repo_root = Path(repo_root)
    out = _run_git(repo_root, ["ls-tree", "-r", "--name-only", ref, "--", area])
    files = [line for line in out.splitlines() if line.strip()]
    logger.info("Area '%s' @ %s: %d arquivo(s) encontrado(s)", area, ref, len(files))
    return files


def grep_symbol(
    repo_root: Path, symbol: str, *, max_results: int = 200
) -> list[GrepHit]:
    repo_root = Path(repo_root)
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "-F", symbol],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VcsError(
            f"Failed to run 'git grep' for {symbol!r} in {repo_root}: {exc}"
        ) from exc

    # git grep exits 1 when there are no matches — not an error for us.
    if result.returncode not in (0, 1):
        raise VcsError(
            f"'git grep' for {symbol!r} in {repo_root} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )

    hits: list[GrepHit] = []
    for line in result.stdout.splitlines()[:max_results]:
        file_part, _, rest = line.partition(":")
        line_no_part, _, text = rest.partition(":")
        if not line_no_part.isdigit():
            continue
        hits.append(
            GrepHit(file=file_part, line_number=int(line_no_part), line_text=text)
        )
    logger.debug("grep_symbol(%r) em %s: %d hit(s)", symbol, repo_root, len(hits))
    return hits
