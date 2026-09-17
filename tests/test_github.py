"""Tests for tools/github.py — never touches the real GitHub API or network.

`gh` CLI calls are faked via monkeypatching subprocess.run; the REST fallback
is faked via respx intercepting httpx.
"""
from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

import httpx
import pytest
import respx

from tools import github


# ------------------------------------------------------------ normalize_repo


@pytest.mark.parametrize(
    "given,expected",
    [
        ("cielo/payments", "cielo/payments"),
        ("https://github.com/cielo/payments", "cielo/payments"),
        ("https://github.com/cielo/payments.git", "cielo/payments"),
        ("https://github.com/cielo/payments/", "cielo/payments"),
        ("git@github.com:cielo/payments.git", "cielo/payments"),
    ],
)
def test_normalize_repo_accepts_multiple_forms(given: str, expected: str) -> None:
    assert github.normalize_repo(given) == expected


def test_normalize_repo_rejects_garbage() -> None:
    with pytest.raises(github.GithubError):
        github.normalize_repo("not-a-repo-at-all")


def test_clone_url_for_always_returns_https() -> None:
    assert (
        github.clone_url_for("git@github.com:cielo/payments.git")
        == "https://github.com/cielo/payments.git"
    )


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def force_no_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh_available", lambda: False)


@pytest.fixture()
def force_has_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh_available", lambda: True)


# --------------------------------------------------------------- via gh CLI


def test_fetch_pr_metadata_via_gh(
    monkeypatch: pytest.MonkeyPatch, force_has_gh: None
) -> None:
    payload = {
        "number": 482,
        "title": "Raise daily limit",
        "baseRefName": "main",
        "headRefName": "feature/raise-daily-limit",
        "baseRefOid": "base123",
        "headRefOid": "head456",
    }

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        assert args[0] == "gh"
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = github.fetch_pr_metadata("cielo/payments", 482)
    assert meta.number == 482
    assert meta.base_sha == "base123"
    assert meta.head_ref == "feature/raise-daily-limit"


def test_fetch_pr_metadata_via_gh_nonzero_exit_raises(
    monkeypatch: pytest.MonkeyPatch, force_has_gh: None
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(1, stderr="pull request not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(github.GithubError, match="pull request not found"):
        github.fetch_pr_metadata("cielo/payments", 999)


def test_fetch_pr_diff_via_gh(
    monkeypatch: pytest.MonkeyPatch, force_has_gh: None
) -> None:
    meta_payload = {
        "number": 482,
        "title": "Raise daily limit",
        "baseRefName": "main",
        "headRefName": "feature/raise-daily-limit",
        "baseRefOid": "base123",
        "headRefOid": "head456",
    }
    files_payload = [
        {
            "filename": "src/main/java/com/cielo/payments/PaymentService.java",
            "status": "modified",
            "patch": "@@ -1,1 +1,1 @@\n-5000\n+8000",
        }
    ]

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        calls.append(args)
        if args[:2] == ["gh", "pr"]:
            return _FakeCompletedProcess(0, stdout=json.dumps(meta_payload))
        return _FakeCompletedProcess(0, stdout=json.dumps(files_payload))

    monkeypatch.setattr(subprocess, "run", fake_run)

    diff = github.fetch_pr_diff("cielo/payments", 482)
    assert diff.source == "github_pr"
    assert diff.repo_root is None
    assert len(diff.files) == 1
    assert diff.files[0].status == "modified"
    assert "8000" in diff.files[0].patch


def test_read_file_at_ref_via_gh(
    monkeypatch: pytest.MonkeyPatch, force_has_gh: None
) -> None:
    content = "public class Foo {}"
    payload = {
        "encoding": "base64",
        "content": base64.b64encode(content.encode()).decode(),
    }

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(0, stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = github.read_file_at_ref("cielo/payments", "main", "Foo.java")
    assert result == content


# ------------------------------------------------------------- REST fallback


@respx.mock
def test_fetch_pr_metadata_via_rest_fallback(force_no_gh: None) -> None:
    respx.get("https://api.github.com/repos/cielo/payments/pulls/482").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 482,
                "title": "Raise daily limit",
                "base": {"ref": "main", "sha": "base123"},
                "head": {"ref": "feature/raise-daily-limit", "sha": "head456"},
            },
        )
    )

    meta = github.fetch_pr_metadata("cielo/payments", 482)
    assert meta.base_sha == "base123"
    assert meta.head_sha == "head456"


@respx.mock
def test_fetch_pr_metadata_via_rest_404_raises(force_no_gh: None) -> None:
    respx.get("https://api.github.com/repos/cielo/payments/pulls/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(github.GithubError, match="404"):
        github.fetch_pr_metadata("cielo/payments", 999)


@respx.mock
def test_read_file_at_ref_via_rest_fallback(force_no_gh: None) -> None:
    content = "public class Foo {}"
    respx.get("https://api.github.com/repos/cielo/payments/contents/Foo.java").mock(
        return_value=httpx.Response(
            200,
            json={
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            },
        )
    )

    result = github.read_file_at_ref("cielo/payments", "main", "Foo.java")
    assert result == content
