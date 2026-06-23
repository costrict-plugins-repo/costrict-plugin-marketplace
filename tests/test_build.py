"""Unit tests for scripts/build.py branch-aware cloning.

Covers the branch-aware additions to `normalize_source_url` / `cache_clone`:
a `/tree/<ref>/...` source_url is now cloned at `<ref>` instead of silently
falling back to the repo's default branch, and an explicit ref (from a catalog
entry's `source_ref`, which can contain slashes) overrides the URL-parsed one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BUILD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "build.py"
_spec = importlib.util.spec_from_file_location("build", _BUILD_PATH)
build = importlib.util.module_from_spec(_spec)
sys.modules["build"] = build
_spec.loader.exec_module(build)


# --------------------------------------------------------------------------- #
# normalize_source_url — three URL shapes
# --------------------------------------------------------------------------- #

class TestNormalizeSourceUrl:
    def test_tree_branch_and_subdir(self):
        clone, sub, branch = build.normalize_source_url(
            "https://github.com/yhangf/csc-plugins/tree/dev/cospowers-requirements"
        )
        assert clone == "https://github.com/yhangf/csc-plugins.git"
        assert sub == "cospowers-requirements"
        assert branch == "dev"

    def test_tree_main_root(self):
        clone, sub, branch = build.normalize_source_url(
            "https://github.com/foo/bar/tree/main"
        )
        assert clone == "https://github.com/foo/bar.git"
        assert sub is None
        assert branch == "main"

    def test_tree_nested_subdir(self):
        clone, sub, branch = build.normalize_source_url(
            "https://github.com/foo/bar/tree/main/plugins/foo"
        )
        assert clone == "https://github.com/foo/bar.git"
        assert sub == "plugins/foo"
        assert branch == "main"

    def test_plain_repo_url_unchanged(self):
        url = "https://github.com/foo/bar"
        assert build.normalize_source_url(url) == (url, None, None)

    def test_plain_repo_git_suffix_unchanged(self):
        url = "https://github.com/foo/bar.git"
        assert build.normalize_source_url(url) == (url, None, None)


# --------------------------------------------------------------------------- #
# cache_clone — ref selection + `--branch` injection (no real network)
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_clone_state():
    build._clone_locks.clear()
    build._clone_failures.clear()
    yield
    build._clone_locks.clear()
    build._clone_failures.clear()


@pytest.fixture
def captured_clone(monkeypatch):
    """Stub subprocess.run so cache_clone 'succeeds' by faking a .git dir.

    Returns the list of argv lists git was invoked with.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        dest = Path(cmd[-1])
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        return None  # check=True path: a non-raising return == success

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    return calls


def _branch_arg(cmd: list[str]) -> str | None:
    return cmd[cmd.index("--branch") + 1] if "--branch" in cmd else None


def test_explicit_branch_overrides_url_branch(tmp_path, captured_clone):
    # URL says main, but an explicit (slash-containing) ref must win.
    build.cache_clone(
        "https://github.com/yhangf/csc-plugins/tree/main/cospowers-requirements",
        tmp_path,
        branch="feat/new-prompt",
    )
    assert _branch_arg(captured_clone[0]) == "feat/new-prompt"


def test_url_branch_used_when_no_explicit(tmp_path, captured_clone):
    build.cache_clone(
        "https://github.com/yhangf/csc-plugins/tree/dev/cospowers-requirements",
        tmp_path,
    )
    assert _branch_arg(captured_clone[0]) == "dev"


def test_no_ref_clones_default_branch(tmp_path, captured_clone):
    # Plain repo URL, no ref anywhere → unchanged behaviour, no --branch.
    build.cache_clone("https://github.com/foo/bar", tmp_path)
    assert "--branch" not in captured_clone[0]


def test_cache_key_isolates_refs(tmp_path, captured_clone):
    # Same repo at two refs must not collide on one cache dir.
    base = "https://github.com/yhangf/csc-plugins/tree/main/cospowers-requirements"
    dest_a = build.cache_clone(base, tmp_path, branch="feat/a")
    dest_b = build.cache_clone(base, tmp_path, branch="feat/b")
    assert dest_a != dest_b
    assert len(captured_clone) == 2  # both actually cloned (no false cache hit)


def test_same_ref_reuses_clone(tmp_path, captured_clone):
    base = "https://github.com/yhangf/csc-plugins/tree/main/cospowers-requirements"
    first = build.cache_clone(base, tmp_path, branch="feat/a")
    second = build.cache_clone(base, tmp_path, branch="feat/a")
    assert first == second
    assert len(captured_clone) == 1  # second call hit the cache


def test_head_sentinel_clones_default_branch(tmp_path, captured_clone):
    # "HEAD" is the official/dev sync's sentinel for "default branch", NOT a real
    # ref — it must never become `git clone --branch HEAD` (which fails). 163 of
    # the current catalog's entries carry bundle.source_ref == "HEAD".
    build.cache_clone("https://github.com/foo/bar.git", tmp_path, branch="HEAD")
    assert "--branch" not in captured_clone[0]


def test_head_in_tree_url_clones_default_branch(tmp_path, captured_clone):
    build.cache_clone("https://github.com/foo/bar/tree/HEAD", tmp_path)
    assert "--branch" not in captured_clone[0]


def test_cache_key_no_collision_for_sanitized_refs(tmp_path, captured_clone):
    # feat/a and feat_a sanitise to the same string — the hash suffix must keep
    # their cache dirs distinct so content never cross-contaminates.
    base = "https://github.com/yhangf/csc-plugins/tree/main/cospowers-requirements"
    dest_slash = build.cache_clone(base, tmp_path, branch="feat/a")
    dest_under = build.cache_clone(base, tmp_path, branch="feat_a")
    assert dest_slash != dest_under
    assert len(captured_clone) == 2
