"""release.sh asset-size guard: fail before touching the GitHub release."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"
VERSION = "9.9.9"


@pytest.fixture
def fake_root(tmp_path):
    """A copy of release.sh under a scratch root plus a gh stub that logs calls."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(RELEASE_SH, tmp_path / "scripts" / "release.sh")
    bundle_dir = tmp_path / "build" / f"costrict-marketplace-bundle-v{VERSION}"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "build-summary.json").write_text("{}")
    (bundle_dir / "manifest.json").write_text("{}")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    # `gh release view` succeeding makes release.sh stop with "already exists" (exit 3),
    # which proves the size guard was passed without publishing anything.
    gh.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/gh-calls.log"\nexit 0\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _make_bundle(root: Path, size: int) -> None:
    with open(root / "build" / f"costrict-marketplace-bundle-v{VERSION}.tar.gz", "wb") as f:
        f.truncate(size)  # sparse: no real disk usage


def _run(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(root / "scripts" / "release.sh"), VERSION],
        capture_output=True, text=True, env=env,
    )


def test_bundle_at_2gib_fails_before_gh(fake_root):
    _make_bundle(fake_root, 2 * 1024**3)
    result = _run(fake_root)
    assert result.returncode == 4
    assert "2 GiB" in result.stderr
    assert not (fake_root / "gh-calls.log").exists()


def test_bundle_over_warn_threshold_warns_and_continues(fake_root):
    _make_bundle(fake_root, int(1.6 * 1024**3))
    result = _run(fake_root)
    assert "::warning::" in result.stdout
    assert result.returncode == 3  # reached `gh release view`
    assert "release view" in (fake_root / "gh-calls.log").read_text()


def test_small_bundle_passes_guard_silently(fake_root):
    _make_bundle(fake_root, 1024)
    result = _run(fake_root)
    assert "::warning::" not in result.stdout
    assert result.returncode == 3
