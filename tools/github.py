"""GitHub PR access: metadata, diff, file-at-ref reads.

Prefers the `gh` CLI (already authenticated in most dev environments); falls
back to the REST API via httpx + GITHUB_TOKEN when `gh` is not installed.
Either path lets `--pr <number>` work without a local clone.
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx

from config.settings import GITHUB_TOKEN
from tools.vcs import DiffResult, FileChange, FileStatus

logger = logging.getLogger("knowledge_agent.github")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API_BASE = "https://api.github.com"

_GH_STATUS_MAP: dict[str, FileStatus] = {
    "added": "added",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "added",
    "changed": "modified",
}


class GithubError(RuntimeError):
    """Raised when GitHub CLI/API access fails or returns something unusable."""


def normalize_repo(repo: str) -> str:
    """Accepts 'owner/name', a full https URL, or an SSH remote and returns
    the bare 'owner/name' form used by the GitHub API/CLI."""
    repo = repo.strip()
    if repo.startswith("http://") or repo.startswith("https://"):
        marker = "github.com/"
        if marker not in repo:
            raise GithubError(f"Unrecognized GitHub URL: {repo!r}")
        tail = repo.split(marker, 1)[1]
    elif repo.startswith("git@"):
        if ":" not in repo:
            raise GithubError(f"Unrecognized GitHub SSH remote: {repo!r}")
        tail = repo.split(":", 1)[1]
    else:
        tail = repo

    tail = tail.strip("/")
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    parts = tail.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise GithubError(f"Could not parse 'owner/name' from: {repo!r}")
    return f"{parts[0]}/{parts[1]}"


def clone_url_for(repo: str) -> str:
    """HTTPS clone URL for a repo given in any of the forms normalize_repo accepts."""
    return f"https://github.com/{normalize_repo(repo)}.git"


@dataclass(frozen=True)
class PrMetadata:
    number: int
    title: str
    repo: str
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _run_gh_json(args: list[str]) -> Any:
    logger.info("gh %s", " ".join(args))
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GithubError(f"Failed to run 'gh {' '.join(args)}': {exc}") from exc
    if result.returncode != 0:
        raise GithubError(
            f"'gh {' '.join(args)}' exited {result.returncode}: {result.stderr.strip()}"
        )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GithubError(
            f"'gh {' '.join(args)}' returned non-JSON output: {result.stdout[:500]!r}"
        ) from exc
    logger.debug("gh %s levou %.2fs", " ".join(args), time.monotonic() - start)
    return parsed


def _rest_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _run_rest_get(path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
    url = f"{_API_BASE}{path}"
    logger.info("GET %s", url)
    start = time.monotonic()
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(url, headers=_rest_headers(), params=params)
    except httpx.HTTPError as exc:
        raise GithubError(f"HTTP transport failure calling {url}: {exc}") from exc

    if response.status_code >= 400:
        raise GithubError(
            f"GitHub API returned HTTP {response.status_code} for {url}: {response.text}"
        )
    try:
        parsed = response.json()
    except json.JSONDecodeError as exc:
        raise GithubError(
            f"GitHub API returned non-JSON body for {url}: {response.text[:500]!r}"
        ) from exc
    logger.debug("GET %s levou %.2fs", url, time.monotonic() - start)
    return parsed


def fetch_pr_metadata(repo: str, pr_number: int) -> PrMetadata:
    if _gh_available():
        data = _run_gh_json(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "number,title,baseRefName,headRefName,baseRefOid,headRefOid",
            ]
        )
        return PrMetadata(
            number=data["number"],
            title=data["title"],
            repo=repo,
            base_ref=data["baseRefName"],
            head_ref=data["headRefName"],
            base_sha=data["baseRefOid"],
            head_sha=data["headRefOid"],
        )

    data = _run_rest_get(f"/repos/{repo}/pulls/{pr_number}")
    try:
        return PrMetadata(
            number=data["number"],
            title=data["title"],
            repo=repo,
            base_ref=data["base"]["ref"],
            head_ref=data["head"]["ref"],
            base_sha=data["base"]["sha"],
            head_sha=data["head"]["sha"],
        )
    except KeyError as exc:
        raise GithubError(
            f"Unexpected PR metadata shape from GitHub REST API: {data!r}"
        ) from exc


def fetch_pr_diff(repo: str, pr_number: int) -> DiffResult:
    metadata = fetch_pr_metadata(repo, pr_number)

    if _gh_available():
        raw_files = _run_gh_json(
            ["api", f"repos/{repo}/pulls/{pr_number}/files", "--paginate"]
        )
    else:
        raw_files = _run_rest_get(
            f"/repos/{repo}/pulls/{pr_number}/files", params={"per_page": 100}
        )

    if not isinstance(raw_files, list):
        raise GithubError(
            f"Unexpected PR files payload shape from GitHub: {raw_files!r}"
        )

    files: list[FileChange] = []
    for entry in raw_files:
        try:
            status = _GH_STATUS_MAP.get(entry["status"], "modified")
            files.append(
                FileChange(
                    path=entry["filename"],
                    status=status,
                    patch=entry.get("patch", ""),
                )
            )
        except KeyError as exc:
            raise GithubError(
                f"Unexpected PR file entry shape from GitHub: {entry!r}"
            ) from exc

    logger.info(
        "PR #%d (%s): %d arquivo(s) alterado(s)", pr_number, repo, len(files)
    )
    return DiffResult(
        source="github_pr",
        base_ref=metadata.base_ref,
        head_ref=metadata.head_ref,
        base_sha=metadata.base_sha,
        head_sha=metadata.head_sha,
        files=files,
        repo_root=None,
    )


def read_file_at_ref(repo: str, ref: str, path: str) -> str:
    if _gh_available():
        data = _run_gh_json(
            ["api", f"repos/{repo}/contents/{path}", "-f", f"ref={ref}"]
        )
    else:
        data = _run_rest_get(f"/repos/{repo}/contents/{path}", params={"ref": ref})

    try:
        if data.get("encoding") != "base64":
            raise GithubError(
                f"Unexpected content encoding for '{path}'@{ref}: {data.get('encoding')!r}"
            )
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except KeyError as exc:
        raise GithubError(
            f"Unexpected file-contents payload shape for '{path}'@{ref}: {data!r}"
        ) from exc
