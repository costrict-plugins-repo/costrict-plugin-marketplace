from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISH_SH = REPO_ROOT / "scripts" / "publish.sh"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def make_plugin_bare(bundle_dir: Path, tmp_path: Path, plugin_id: str, content: str) -> None:
    work = tmp_path / f"work-{plugin_id}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    (work / ".claude-plugin").mkdir()
    (work / ".claude-plugin" / "plugin.json").write_text(f'{{"name":"{plugin_id}"}}\n')
    (work / "README.md").write_text(content)
    run(["git", "init", "-b", "main", "--quiet"], cwd=work)
    run(["git", "config", "user.name", "test"], cwd=work)
    run(["git", "config", "user.email", "test@example.com"], cwd=work)
    run(["git", "add", "-A"], cwd=work)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00+00:00"
    env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00+00:00"
    run(["git", "commit", "--quiet", "-m", f"init {plugin_id}"], cwd=work, env=env)

    bare = bundle_dir / "repos" / "plugins" / f"{plugin_id}.git"
    run(["git", "clone", "--bare", "--quiet", str(work), str(bare)])
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare)


def make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "repos" / "plugins").mkdir(parents=True)
    (bundle / "manifest.json").write_text('{"bundle_version":"test"}\n')
    (bundle / "marketplace.json.tmpl").write_text('{"plugins":[{"source":{"url":"{{BASE_URL}}/plugin-a.git"}}]}\n')
    make_plugin_bare(bundle, tmp_path, "plugin-a", "one\n")
    make_plugin_bare(bundle, tmp_path, "plugin-b", "two\n")
    return bundle


def publish(bundle: Path, target: Path) -> str:
    result = run(
        [
            str(PUBLISH_SH),
            str(bundle),
            "--local-target-dir",
            str(target),
            "--yes",
            "--parallel",
            "2",
            "--skip-marketplace",
        ]
    )
    return result.stdout + result.stderr


def test_publish_skips_repos_with_matching_main_tree(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    target = tmp_path / "target"

    first = publish(bundle, target)
    assert "Pushed:  2 / 2" in first
    assert "Failed:  0" in first

    shutil.rmtree(bundle / "repos" / "plugins" / "plugin-a.git")
    make_plugin_bare(bundle, tmp_path, "plugin-a", "one\n")

    second = publish(bundle, target)
    assert "Pushed:  0 / 2" in second
    assert "Skipped: 2 (already up-to-date)" in second
    assert "Failed:  0" in second


def test_publish_pushes_only_changed_repo(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    target = tmp_path / "target"
    publish(bundle, target)

    old = bundle / "repos" / "plugins" / "plugin-a.git"
    shutil.rmtree(old)
    make_plugin_bare(bundle, tmp_path, "plugin-a", "changed\n")

    output = publish(bundle, target)
    assert "Pushed:  1 / 2" in output
    assert "Skipped: 1 (already up-to-date)" in output
    assert "Failed:  0" in output

    clone = tmp_path / "clone-plugin-a"
    run(["git", "clone", "--quiet", str(target / "plugin-a.git"), str(clone)])
    assert (clone / "README.md").read_text() == "changed\n"
