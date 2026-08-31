"""Search scope: narrow "all projects" down to "global notes + current project".

Enabled by the BM_SCOPE_FILE env var pointing at a scope.json registry
({"globals": [...vault names], "projects": {vault name: source repo path}}).
When unset, this module is fully inert and behavior matches upstream.

Resolution (highest priority first):
1. env BM_SEARCH_PROJECTS (comma-separated) — explicit allowlist, used as-is,
   cwd is not consulted.
2. Derive the current project from cwd, union with globals
   (cwd source: BM_SCOPE_CWD env > os.getcwd(); the former exists for tests):
   a. Path prefix match: cwd falls inside a registered source repo
      (subdirectories count).
   b. Worktree fold: cwd containing /.claude/worktrees/ is truncated back to
      the main repo path (Claude Code convention); fallback uses
      `git rev-parse --git-common-dir` to cover worktrees at any location.
   c. Derived clones: cwd's git origin URL matches a registered repo's origin
      (compared after normalization) — treated as a variant of that project.
      Note: scp-style SSH (git@host:path/repo) and HTTP (host/path/repo) do
      NOT normalize equal; repos with mixed remote styles need explicit
      registration in scope.json.
   d. None of the above → globals only. Rather miss a project than search the
      wrong one: cross-project noise was measured (hgci beat glci for top-1
      by 0.001), while a miss only costs generic notes.

Unregistered vaults never participate in search.
If BM_SCOPE_FILE is set but the registry is unreadable, the scope is EMPTY —
a loud failure, not a silent fallback to "search everything".
"""

import functools
import json
import os
import re
import subprocess

from loguru import logger as log

_WORKTREE_MARK = "/.claude/worktrees/"


def _load_registry() -> tuple[list[str], dict[str, str]] | None:
    """Three states: None=scope disabled (BM_SCOPE_FILE unset);
    (globals, projects)=loaded; ([], {})=registry unreadable (loud failure)."""
    path = os.environ.get("BM_SCOPE_FILE")
    if not path:
        return None
    try:
        with open(path) as f:
            reg = json.load(f)
        return list(reg.get("globals", [])), dict(reg.get("projects", {}))
    except (OSError, ValueError) as e:
        log.error("scope registry unusable ({}); search returns empty instead of all projects", e)
        return [], {}


def _git(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return out or None


def _norm_origin(url: str) -> str:
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip().lower())
    url = re.sub(r"^[^/@]+@", "", url)
    return re.sub(r"\.git$", "", url.rstrip("/"))


@functools.lru_cache(maxsize=64)
def _origin_of(path: str) -> str | None:
    url = _git(path, "remote", "get-url", "origin")
    return _norm_origin(url) if url else None


def _match_by_prefix(cwd: str, projects: dict[str, str]) -> str | None:
    best: tuple[int, str] | None = None
    for vault, path in projects.items():
        path = os.path.realpath(path)
        if cwd == path or cwd.startswith(path + os.sep):
            if best is None or len(path) > best[0]:
                best = (len(path), vault)
    return best[1] if best else None


def resolve_scope(cwd: str | None = None) -> tuple[list[str] | None, str]:
    """Return (vault names in scope, hit reason); names=None means scope disabled."""
    reg = _load_registry()
    if reg is None:
        return None, "disabled"
    globals_, projects = reg
    if not globals_ and not projects:
        return [], "registry-missing"

    cwd = os.path.realpath(cwd or os.environ.get("BM_SCOPE_CWD") or os.getcwd())

    hit = _match_by_prefix(cwd, projects)
    if hit:
        return sorted(set(globals_) | {hit}), f"prefix:{hit}"

    # Worktree fold: Claude Code path convention, truncate back to main repo
    if _WORKTREE_MARK in cwd:
        hit = _match_by_prefix(cwd.split(_WORKTREE_MARK)[0], projects)
        if hit:
            return sorted(set(globals_) | {hit}), f"worktree-path:{hit}"

    # Git worktree at any location: common dir points back to main repo .git
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common and common.endswith("/.git"):
        hit = _match_by_prefix(os.path.dirname(common), projects)
        if hit:
            return sorted(set(globals_) | {hit}), f"worktree-git:{hit}"

    # Derived clone: same origin URL = variant of the same project
    origin = _origin_of(cwd)
    if origin:
        for vault, path in projects.items():
            if _origin_of(os.path.realpath(path)) == origin:
                return sorted(set(globals_) | {vault}), f"origin:{vault}"

    return sorted(set(globals_)), "globals-only"


def scoped_projects() -> list[str] | None:
    """Allowlist for _load_search_project_refs; None means scope disabled (upstream behavior)."""
    raw = os.environ.get("BM_SEARCH_PROJECTS", "").strip()
    if raw:
        return sorted({p.strip() for p in raw.split(",") if p.strip()})
    allow, why = resolve_scope()
    if allow is not None:
        log.info("bm scope: {} ({})", allow, why)
    return allow
