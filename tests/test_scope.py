"""scope 模块的验证：纯路径推导 + 真实 git worktree / 派生仓。

真实 git 用例会自建临时仓库再清理，不动现有仓库。
"""

import os
import shutil
import subprocess

import pytest

from basic_memory.scope import resolve_scope

# 本机注册表：mem-project → agentforge 主仓
AF = "/home/llm/vibe_coding/agentforge"
AF_ORIGIN = "http://8.160.168.184:10010/gitlab/agentforge-team/agentforge.git"
HTY = "/home/llm/vibe_coding/hty-knowledge-graph"

SCOPE_JSON = "/home/llm/basic-memory/scope.json"


@pytest.fixture(autouse=True)
def _scope_env(monkeypatch):
    """每个用例都在注册的 scope 下跑，结束恢复原环境。"""
    monkeypatch.setenv("BM_SCOPE_FILE", SCOPE_JSON)
    monkeypatch.delenv("BM_SEARCH_PROJECTS", raising=False)
    monkeypatch.delenv("BM_SCOPE_CWD", raising=False)


def check(name, cwd, want_vaults, want_why_prefix, failures):
    got, why = resolve_scope(cwd)
    ok = got == want_vaults and why.startswith(want_why_prefix)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got} ({why})")
    if not ok:
        failures.append(f"{name}: got={got} ({why}), want={want_vaults} ({want_why_prefix}*)")


@pytest.mark.skipif(not os.path.isdir(AF), reason="agentforge 主仓不在本机")
def test_pure_path_derivation():
    failures = []
    check("主仓根目录", AF, ["mem-global", "mem-project"], "prefix", failures)
    check("主仓子目录", AF + "/backend", ["mem-global", "mem-project"], "prefix", failures)
    check(
        "CC 约定 worktree 路径(不存在也认)",
        AF + "/.claude/worktrees/split-pg",
        ["mem-global", "mem-project"],
        "prefix",
        failures,
    )
    check("hty 仓", HTY, ["mem-global", "mem-hty"], "prefix", failures)
    check("无 vault 的目录", "/home/llm/im-agent-hub", ["mem-global"], "globals-only", failures)
    assert not failures, failures


@pytest.mark.skipif(not os.path.isdir(AF), reason="agentforge 主仓不在本机")
def test_real_git_variants(tmp_path):
    failures = []
    wt = str(tmp_path / "wt")
    derived = str(tmp_path / "derived")
    forkish = str(tmp_path / "forkish")

    def sh(*a, cwd=None):
        subprocess.run(a, cwd=cwd, check=True, capture_output=True)

    subprocess.run(["git", "-C", AF, "worktree", "add", "--detach", wt], check=True, capture_output=True)
    try:
        check("真 worktree(任意位置, 走 git-common-dir)", wt, ["mem-global", "mem-project"], "worktree-git", failures)

        os.makedirs(derived)
        sh("git", "init", "-q", cwd=derived)
        sh("git", "remote", "add", "origin", AF_ORIGIN, cwd=derived)
        check("派生仓(同 origin, 未克隆)", derived, ["mem-global", "mem-project"], "origin", failures)

        os.makedirs(forkish)
        sh("git", "init", "-q", cwd=forkish)
        sh("git", "remote", "add", "origin", AF_ORIGIN.replace("agentforge.git", "agentforge-fork.git"), cwd=forkish)
        check("fork(不同 origin, 判不出)", forkish, ["mem-global"], "globals-only", failures)
    finally:
        subprocess.run(["git", "-C", AF, "worktree", "remove", "--force", wt], capture_output=True)
        shutil.rmtree(derived, ignore_errors=True)
        shutil.rmtree(forkish, ignore_errors=True)
        leftover = subprocess.run(
            ["git", "-C", AF, "worktree", "list"], capture_output=True, text=True
        ).stdout
        assert wt not in leftover, "worktree 没清干净"
    assert not failures, failures


def test_registry_missing_returns_empty(monkeypatch):
    monkeypatch.setenv("BM_SCOPE_FILE", "/tmp/bm-no-such-scope.json")
    got, why = resolve_scope(AF)
    assert got == [] and why == "registry-missing"


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("BM_SCOPE_FILE", raising=False)
    got, why = resolve_scope(AF)
    assert got is None and why == "disabled"


def test_manual_override(monkeypatch):
    from basic_memory.scope import scoped_projects

    monkeypatch.setenv("BM_SEARCH_PROJECTS", "mem-global, mem-project")
    assert scoped_projects() == ["mem-global", "mem-project"]


def test_request_scope_cwd_stdio_has_no_http_request(monkeypatch):
    """No live HTTP request (stdio MCP) → None → resolve_scope uses process getcwd.

    Shared-server HTTP is opt-in via the header; stdio must not change.
    """
    from basic_memory.scope import request_scope_cwd

    def _no_request(include=None):
        return {}

    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        _no_request,
        raising=False,
    )
    assert request_scope_cwd() is None


def test_request_scope_cwd_reads_absolute_header(monkeypatch):
    from basic_memory.scope import SCOPE_CWD_HEADER, request_scope_cwd, scoped_projects

    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {SCOPE_CWD_HEADER: AF},
        raising=False,
    )
    assert request_scope_cwd() == AF
    # Header cwd must beat process getcwd (the shared-server failure mode).
    monkeypatch.chdir("/tmp")
    monkeypatch.delenv("BM_SCOPE_CWD", raising=False)
    assert scoped_projects(request_scope_cwd()) == ["mem-global", "mem-project"]


@pytest.mark.parametrize(
    "raw",
    ["relative/path", "tmp", "foo\n/tmp", ""],
)
def test_request_scope_cwd_ignores_non_absolute_or_empty(monkeypatch, raw):
    from basic_memory.scope import SCOPE_CWD_HEADER, request_scope_cwd

    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda include=None: {SCOPE_CWD_HEADER: raw} if raw else {},
        raising=False,
    )
    assert request_scope_cwd() is None


def test_scoped_projects_explicit_cwd_beats_process_getcwd(monkeypatch):
    from basic_memory.scope import scoped_projects

    monkeypatch.chdir("/tmp")
    monkeypatch.delenv("BM_SCOPE_CWD", raising=False)
    assert scoped_projects("/tmp") == ["mem-global"]
    assert scoped_projects(AF) == ["mem-global", "mem-project"]
