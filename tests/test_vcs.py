"""Tests for tools/vcs.py against a real, ephemeral git repo (tmp_java_repo)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import vcs


def test_diff_branches_reports_modified_file(tmp_java_repo: Path) -> None:
    diff = vcs.diff_branches(tmp_java_repo, "main", "feature/raise-daily-limit")

    assert diff.source == "local_git"
    assert diff.base_sha != diff.head_sha
    paths = {f.path for f in diff.files}
    assert "src/main/java/com/cielo/payments/PaymentService.java" in paths

    changed = next(f for f in diff.files if f.path.endswith("PaymentService.java"))
    assert changed.status == "modified"
    assert "8000" in changed.patch


def test_diff_branches_unknown_ref_raises(tmp_java_repo: Path) -> None:
    with pytest.raises(vcs.VcsError):
        vcs.diff_branches(tmp_java_repo, "main", "does-not-exist")


def test_read_file_at_ref_reads_old_and_new_content(tmp_java_repo: Path) -> None:
    old = vcs.read_file_at_ref(
        tmp_java_repo, "main", "src/main/java/com/cielo/payments/PaymentService.java"
    )
    new = vcs.read_file_at_ref(
        tmp_java_repo,
        "feature/raise-daily-limit",
        "src/main/java/com/cielo/payments/PaymentService.java",
    )
    assert "5000" in old
    assert "8000" in new


def test_read_file_at_ref_missing_path_raises(tmp_java_repo: Path) -> None:
    with pytest.raises(vcs.VcsError):
        vcs.read_file_at_ref(tmp_java_repo, "main", "no/such/file.java")


def test_list_files_in_area(tmp_java_repo: Path) -> None:
    files = vcs.list_files_in_area(tmp_java_repo, "src/main/java/com/cielo/payments")
    assert any(f.endswith("PaymentController.java") for f in files)
    assert any(f.endswith("PaymentService.java") for f in files)


def test_grep_symbol_finds_usage(tmp_java_repo: Path) -> None:
    hits = vcs.grep_symbol(tmp_java_repo, "paymentService.authorize")
    assert len(hits) >= 1
    assert any("PaymentController.java" in h.file for h in hits)


def test_grep_symbol_no_matches_returns_empty(tmp_java_repo: Path) -> None:
    assert vcs.grep_symbol(tmp_java_repo, "ThisSymbolDoesNotExistAnywhere") == []


# ------------------------------------------------------------ clone_or_update


def test_clone_or_update_clones_when_missing(tmp_java_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "clone-dest"
    result = vcs.clone_or_update(str(tmp_java_repo), dest)

    assert result == dest
    assert (dest / ".git").is_dir()
    assert (dest / "README.md").exists()


def test_clone_or_update_fetches_when_already_cloned(tmp_java_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "clone-dest"
    vcs.clone_or_update(str(tmp_java_repo), dest)

    # New commit on the "remote" (tmp_java_repo) after the first clone.
    (tmp_java_repo / "NEW_FILE.md").write_text("novo arquivo\n", encoding="utf-8")
    subprocess_run = __import__("subprocess").run
    subprocess_run(["git", "add", "-A"], cwd=tmp_java_repo, check=True, capture_output=True)
    subprocess_run(
        ["git", "commit", "-m", "add NEW_FILE.md"],
        cwd=tmp_java_repo,
        check=True,
        capture_output=True,
        env=None,
    )

    result = vcs.clone_or_update(str(tmp_java_repo), dest)
    assert result == dest
    # fetch pulls refs but doesn't merge into the checked-out branch —
    # confirm the new commit is now reachable in the clone's object store.
    out = vcs._run_git(dest, ["log", "--all", "--oneline"])
    assert "add NEW_FILE.md" in out


def test_clone_or_update_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(vcs.VcsError):
        vcs.clone_or_update(str(tmp_path / "does-not-exist"), tmp_path / "dest")
