#!/usr/bin/env python3
"""Build pipeline for the costrict-plugin-marketplace bundle.

Reads the `costrict-skills-repo` plugin catalog, fetches per-plugin content,
prunes to runtime essentials, materialises one bare git repo per plugin,
generates a marketplace.json template + bundle manifest, and tars everything
into `costrict-marketplace-bundle-v<semver>.tar.gz`.

Optional `--publish` pushes each bare repo to the `costrict-plugins-repo`
GitHub org (creating the repo if absent). The tar.gz is always produced
locally regardless of publish state.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from typing import Iterable
from urllib.request import Request, urlopen

LOG = logging.getLogger("build")

MARKETPLACE_NAME = "costrict-plugins"
GITHUB_ORG_DEFAULT = "costrict-plugins-repo"
BUILD_AUTHOR_NAME = "costrict-build"
BUILD_AUTHOR_EMAIL = "build@costrict.local"
MEDIA_SIZE_THRESHOLD = 500 * 1024

# Denylist pruning (see research/prune-impact-fix.md). build.py clones with
# `--depth 1` and strips `.git`, so node_modules/dist/build/.next never enter the
# tree unless committed — the old whitelist instead deleted FUNCTIONAL content
# (.mcp.json, scripts/, src/, lib/, rules/, templates/, CLAUDE.md, hooks scripts).
# We now KEEP everything except known dev/CI noise + caches, and rely on the
# whole-tree large-media sweep for the real size savings.
DROP_DIRS = {
    "node_modules", "dist", "build", "out", "target", ".next", "coverage",
    ".turbo", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", ".git", ".github", ".idea", ".vscode", ".zed",
    ".husky", ".changeset", ".cache",
}
DROP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "uv.lock", "Pipfile.lock", "composer.lock",
    "Gemfile.lock", "go.sum", ".DS_Store",
}
MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".m4v"}
# Large non-media binaries also swept past the size threshold.
LARGE_BIN_EXTS = {".pdf", ".zip", ".tar", ".gz", ".tgz", ".7z", ".bin", ".onnx", ".db", ".sqlite"}


@dataclass
class BuildContext:
    catalog_path: Path
    catalog_source: str
    catalog_sha: str
    catalog_bundle_sha: str | None
    output_dir: Path
    bundle_root: Path
    cache_dir: Path
    version: str
    build_date_iso: str
    plugin_count_limit: int | None
    github_org: str
    publish: bool
    failures: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    skipped_unverified: int = 0
    included: list[dict] = field(default_factory=list)


@dataclass
class FetchedPlugin:
    entry: dict
    work_dir: Path  # pruned content lives here


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    LOG.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True, env=env)


def load_catalog(catalog_path: Path) -> list[dict]:
    return json.loads(catalog_path.read_text())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def catalog_sha(catalog_path: Path) -> str:
    return hashlib.sha256(catalog_path.read_bytes()).hexdigest()


def normalize_sha(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value.split()[0] if value else ""


def download_file(url: str, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / filename
    headers = {"User-Agent": "costrict-plugin-marketplace-build"}
    token = os.environ.get("CATALOG_DOWNLOAD_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(("https://github.com/", "https://api.github.com/", "https://raw.githubusercontent.com/")):
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/octet-stream"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=120) as response:
        dest.write_bytes(response.read())
    return dest


def download_catalog(catalog_url: str, output_dir: Path) -> Path:
    return download_file(catalog_url, output_dir, "plugins-index.json")


def _safe_tar_members(bundle_path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(bundle_path, "r:*") as tar:
        return {m.name: m for m in tar.getmembers() if m.isfile() and not Path(m.name).is_absolute() and ".." not in Path(m.name).parts}


def _read_tar_member(bundle_path: Path, member_name: str) -> bytes:
    with tarfile.open(bundle_path, "r:*") as tar:
        extracted = tar.extractfile(member_name)
        if extracted is None:
            raise ValueError(f"catalog bundle member is not readable: {member_name}")
        return extracted.read()


def _pick_catalog_bundle_members(members: dict[str, tarfile.TarInfo]) -> tuple[str, str]:
    def parts(name: str) -> tuple[str, ...]:
        return tuple(Path(name).parts)

    manifest_candidates = [name for name in members if parts(name)[-1:] == ("manifest.json",) and len(parts(name)) <= 2]
    if not manifest_candidates:
        raise ValueError("catalog bundle missing manifest.json")
    manifest_name = sorted(manifest_candidates, key=lambda n: (len(parts(n)), n))[0]
    prefix = Path(manifest_name).parent
    index_name = str(prefix / "index.json") if str(prefix) != "." else "index.json"
    if index_name not in members:
        index_candidates = [name for name in members if parts(name)[-1:] == ("index.json",) and len(parts(name)) <= 2]
        if not index_candidates:
            raise ValueError("catalog bundle missing index.json")
        index_name = sorted(index_candidates, key=lambda n: (len(parts(n)), n))[0]
    return manifest_name, index_name


def load_catalog_bundle(bundle_path: Path) -> tuple[list[dict], str, dict[str, Any], int]:
    members = _safe_tar_members(bundle_path)
    manifest_name, index_name = _pick_catalog_bundle_members(members)
    manifest = json.loads(_read_tar_member(bundle_path, manifest_name))
    index_bytes = _read_tar_member(bundle_path, index_name)
    actual_index_sha = sha256_bytes(index_bytes)
    declared_index_sha = normalize_sha(str(manifest.get("index_sha256", "")))
    if declared_index_sha and declared_index_sha != actual_index_sha:
        raise ValueError(f"catalog bundle index_sha256 mismatch: manifest={declared_index_sha} actual={actual_index_sha}")
    entries = json.loads(index_bytes)
    if not isinstance(entries, list):
        raise ValueError("catalog bundle index.json must be a JSON array")
    plugins = [e for e in entries if isinstance(e, dict) and e.get("type") == "plugin"]
    return plugins, actual_index_sha, manifest, len(entries)


def filter_verified(entries: list[dict]) -> tuple[list[dict], int]:
    """Apply spec rule: include only marketplace_verified=true; warn-and-include-all when field absent."""
    has_field = any("marketplace_verified" in e.get("install", {}) for e in entries)
    if not has_field:
        LOG.warning("marketplace_verified field missing, processing all entries")
        return list(entries), 0
    kept = [e for e in entries if e.get("install", {}).get("marketplace_verified") is True]
    skipped = len(entries) - len(kept)
    LOG.info("verified-only mode: kept=%d skipped=%d", len(kept), skipped)
    return kept, skipped


_TREE_RE = re.compile(
    r"^(?P<base>https?://github\.com/[^/]+/[^/]+?)(?:\.git)?/tree/(?P<branch>[^/]+)(?:/(?P<sub>.*))?$"
)


def normalize_source_url(source_url: str) -> tuple[str, str | None, str | None]:
    """Return (clonable_url, hinted_subdir, branch).

    Handles GitHub web-UI URLs like `.../tree/main` and
    `.../tree/main/plugins/foo` that aren't clonable directly. Root tree URLs
    produce no subdirectory hint; nested tree URLs return the nested path as a
    hint for find_plugin_root.

    `branch` is the ref parsed from the `/tree/<ref>/` segment so the clone can
    target it (previously discarded → every tree URL silently cloned the default
    branch). Only a single path segment is captured here; refs that contain a
    slash (e.g. `feat/foo`) are ambiguous in a web URL, so callers that know the
    ref unambiguously (from a catalog entry's `source_ref`) should pass it
    explicitly to `cache_clone` rather than rely on this.
    """
    m = _TREE_RE.match(source_url)
    if m:
        subdir = (m.group("sub") or "").strip("/")
        return m.group("base") + ".git", subdir or None, m.group("branch")
    return source_url, None, None


_clone_locks: dict[str, threading.Lock] = {}
_clone_locks_master = threading.Lock()
_clone_failures: dict[str, str] = {}
_ctx_lock = threading.Lock()

CLONE_TIMEOUT_SECONDS = 90  # individual git clone hard timeout


class CloneFailedAndCached(Exception):
    """A previously-attempted clone for this URL failed; do not retry within this run."""


def cache_clone(source_url: str, cache_dir: Path, branch: str | None = None) -> Path:
    """Clone (or reuse) source_url under cache_dir; thread-safe per (url, ref).

    `branch` (when given) is the authoritative ref to check out — it wins over
    any ref parsed from the URL and is the only way to clone a slash-containing
    ref unambiguously. With no ref from either source the default branch is
    cloned (unchanged behaviour).

    Optimisations: failed (url, ref) pairs are remembered so concurrent plugins
    sharing a bad source short-circuit instead of stampeding the same network
    call. The cache/lock identity includes the ref so the same repo cloned at
    two refs never collides on one cache dir.
    """
    clone_url, _, url_branch = normalize_source_url(source_url)
    ref = branch or url_branch
    if ref == "HEAD":  # sentinel for "default branch" (official/dev sync), not a real ref
        ref = None
    clone_id = clone_url if not ref else f"{clone_url}#{ref}"
    with _clone_locks_master:
        lock = _clone_locks.setdefault(clone_id, threading.Lock())
    with lock:
        # short-circuit on prior failure
        if clone_id in _clone_failures:
            raise CloneFailedAndCached(_clone_failures[clone_id])
        # Sanitising for the filesystem is lossy (feat/a and feat_a collapse to
        # the same name), so append a hash of the exact clone_id to keep every
        # (url, ref) pair in its own cache dir.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", clone_id)[:100]
        safe = f"{safe}__{hashlib.sha256(clone_id.encode()).hexdigest()[:12]}"
        dest = cache_dir / safe
        if dest.exists() and (dest / ".git").exists():
            return dest
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        LOG.debug("clone %s%s", clone_url, f" @ {ref}" if ref else "")
        cmd = [
            "git",
            "-c",
            "http.lowSpeedLimit=1000",
            "-c",
            "http.lowSpeedTime=20",
            "clone",
            "--depth",
            "1",
            "--quiet",
        ]
        if ref:
            cmd += ["--branch", ref]
        cmd += [clone_url, str(dest)]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(dest, ignore_errors=True)
            _clone_failures[clone_id] = "timeout"
            raise CloneFailedAndCached("clone timed out") from None
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            _clone_failures[clone_id] = (exc.stderr or "").strip()[:200] or "git clone failed"
            raise CloneFailedAndCached(_clone_failures[clone_id]) from None
        return dest


def find_plugin_root(clone_dir: Path, plugin_name: str, subdir_hint: str | None = None) -> Path | None:
    """Locate `.claude-plugin/plugin.json` within a cloned repo.

    Tries (subdir_hint, root, plugins/<name>, <name>) in that order, then a
    wide rglob fallback.
    """
    candidates: list[Path] = []
    if subdir_hint:
        candidates.append(clone_dir / subdir_hint)
    candidates += [clone_dir, clone_dir / "plugins" / plugin_name, clone_dir / plugin_name]
    for c in candidates:
        if (c / ".claude-plugin" / "plugin.json").is_file():
            return c
    for p in clone_dir.rglob(".claude-plugin/plugin.json"):
        return p.parent.parent
    return None


def fetch_plugin_content(entry: dict, work_dir: Path, ctx: BuildContext) -> Path | None:
    """Clone source_url (cached) and copy the plugin's content to work_dir."""
    source_url = entry.get("source_url")
    plugin_name = entry.get("install", {}).get("plugin_name") or entry.get("name")
    if not source_url:
        raise ValueError("missing source_url")
    _, url_subdir, _ = normalize_source_url(source_url)
    # Prefer the producer's unambiguous fields (set by everything-ai-coding's
    # sync_plugins_csc.py): bundle.source_ref pins the exact ref (works for
    # slash-containing branches the URL can't disambiguate), bundle.plugin_root
    # the exact subdir. Fall back to whatever the URL yields for plain entries.
    bundle = entry.get("bundle") or {}
    # source_ref is the producer's authoritative ref. "HEAD"/"" are sentinels for
    # "default branch" (set by the official/dev sync for the bulk of the catalog),
    # NOT real refs — leave them as None so the clone keeps using the default
    # branch instead of `git clone --branch HEAD` (which fails).
    branch = bundle.get("source_ref") or entry.get("source_ref")
    if branch in ("", "HEAD"):
        branch = None
    subdir_hint = bundle.get("plugin_root") or url_subdir
    cloned = cache_clone(source_url, ctx.cache_dir, branch=branch)
    root = find_plugin_root(cloned, plugin_name, subdir_hint=subdir_hint)
    if root is None:
        return None
    if work_dir.exists():
        shutil.rmtree(work_dir)
    shutil.copytree(root, work_dir, ignore=shutil.ignore_patterns(".git"))
    return work_dir


def prune_plugin_content(plugin_dir: Path) -> None:
    """Denylist prune: drop only known dev/CI noise + caches, keep all runtime
    content (.mcp.json, scripts/, src/, lib/, rules/, templates/, hooks, …),
    then sweep large media/binaries from the whole tree.
    """
    if not plugin_dir.exists():
        return
    for entry in list(plugin_dir.iterdir()):
        name = entry.name
        if entry.is_dir():
            if name in DROP_DIRS:
                shutil.rmtree(entry)
            # everything else is kept verbatim
        else:
            if name in DROP_FILES:
                entry.unlink()
            # all other top-level files are kept (.mcp.json, package.json, configs)
    # Real size lever: drop oversized media/binaries across the entire retained
    # tree (the old code only swept inside the 5 whitelisted dirs).
    _prune_large_media(plugin_dir)


def _prune_large_media(dir_: Path) -> None:
    for f in dir_.rglob("*"):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in MEDIA_EXTS and ext not in LARGE_BIN_EXTS:
            continue
        try:
            too_big = f.stat().st_size > MEDIA_SIZE_THRESHOLD
        except OSError:
            continue
        if too_big:
            f.unlink()


def validate_plugin(plugin_dir: Path) -> bool:
    return (plugin_dir / ".claude-plugin" / "plugin.json").is_file()


def create_bare_repo(plugin_dir: Path, plugin_id: str, version: str, ctx: BuildContext) -> Path:
    """Init a bare repo with one reproducible commit containing the pruned content."""
    bare = ctx.bundle_root / "repos" / "plugins" / f"{plugin_id}.git"
    if bare.exists():
        shutil.rmtree(bare)
    bare.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": BUILD_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": BUILD_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": BUILD_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": BUILD_AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": ctx.build_date_iso,
            "GIT_COMMITTER_DATE": ctx.build_date_iso,
        }
    )
    run(["git", "init", "-b", "main", "--quiet"], cwd=plugin_dir, env=env)
    run(["git", "add", "-A"], cwd=plugin_dir, env=env)
    run(
        [
            "git",
            "commit",
            "--quiet",
            "-m",
            f"costrict-plugin-marketplace bundle v{version}",
        ],
        cwd=plugin_dir,
        env=env,
    )
    run(["git", "clone", "--bare", "--quiet", str(plugin_dir), str(bare)], env=env)
    # set HEAD symbolic ref to main
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare)
    return bare


def plugin_size_bytes(bare_dir: Path) -> int:
    total = 0
    for f in bare_dir.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def process_plugin(entry: dict, ctx: BuildContext) -> dict | None:
    plugin_id = entry["id"]
    bare = ctx.bundle_root / "repos" / "plugins" / f"{plugin_id}.git"
    # Resume: if a bare repo with the expected single commit already exists
    # from a prior run, reuse it. Saves time on restart after partial builds.
    if (bare / "HEAD").is_file() and (bare / "refs" / "heads" / "main").is_file():
        return {
            "id": plugin_id,
            "name": entry.get("name", plugin_id),
            "version": entry.get("version") or "0.0.0",
            "size_bytes": plugin_size_bytes(bare),
            "category": entry.get("category", "other"),
            "description": entry.get("description", ""),
        }
    work_dir = ctx.bundle_root / "_tmp_work" / plugin_id
    try:
        fetched = fetch_plugin_content(entry, work_dir, ctx)
    except Exception as exc:
        LOG.warning("fetch failed: %s (%s)", plugin_id, exc)
        with _ctx_lock:
            ctx.failures.append({"plugin_id": plugin_id, "reason": f"fetch: {exc}"})
        return None
    if fetched is None:
        LOG.warning("plugin.json not found: %s", plugin_id)
        with _ctx_lock:
            ctx.invalid.append({"plugin_id": plugin_id, "reason": "no .claude-plugin/plugin.json"})
        return None
    # Per-entry opt-out: first-party plugins set `prune_content: false` to be
    # mirrored verbatim (only .git is stripped, by fetch_plugin_content). This
    # preserves rules/, templates/, evaluators/, examples/, CLAUDE.md, etc. that
    # their skills depend on. Default (field absent) keeps the size-pruning
    # behaviour for the bulk public catalog.
    if entry.get("prune_content") is False:
        LOG.info("full-content mirror (prune skipped): %s", plugin_id)
    else:
        try:
            prune_plugin_content(fetched)
        except Exception as exc:
            LOG.warning("prune failed: %s (%s)", plugin_id, exc)
            with _ctx_lock:
                ctx.failures.append({"plugin_id": plugin_id, "reason": f"prune: {exc}"})
            return None
    if not validate_plugin(fetched):
        LOG.warning("post-prune validation failed: %s", plugin_id)
        with _ctx_lock:
            ctx.invalid.append({"plugin_id": plugin_id, "reason": "post-prune missing plugin.json"})
        return None
    try:
        bare = create_bare_repo(fetched, plugin_id, ctx.version, ctx)
    except Exception as exc:
        LOG.warning("bare init failed: %s (%s)", plugin_id, exc)
        with _ctx_lock:
            ctx.failures.append({"plugin_id": plugin_id, "reason": f"bare: {exc}"})
        return None
    finally:
        shutil.rmtree(fetched, ignore_errors=True)
    return {
        "id": plugin_id,
        "name": entry.get("name", plugin_id),
        "version": entry.get("version") or "0.0.0",
        "size_bytes": plugin_size_bytes(bare),
        "category": entry.get("category", "other"),
        "description": entry.get("description", ""),
    }


def generate_marketplace_template(plugins: list[dict], ctx: BuildContext) -> dict:
    """Build the marketplace.json structure with `{{BASE_URL}}` placeholders."""
    return {
        "name": MARKETPLACE_NAME,
        "owner": {
            "name": "Costrict",
            "url": f"https://github.com/{ctx.github_org}",
        },
        "description": "Mirror of the costrict-skills-repo plugin catalog for private deployments.",
        "plugins": [
            {
                "name": p["name"],
                "description": p["description"],
                "version": p["version"],
                "category": p["category"],
                "source": {
                    "source": "url",
                    "url": f"{{{{BASE_URL}}}}/{p['id']}.git",
                },
            }
            for p in sorted(plugins, key=lambda x: x["id"])
        ],
    }


def write_template(template: dict, ctx: BuildContext) -> Path:
    out = ctx.bundle_root / "marketplace.json.tmpl"
    out.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
    json.loads(out.read_text())  # self-check (placeholders inside string values are valid)
    return out


def write_manifest(plugins: list[dict], ctx: BuildContext) -> Path:
    manifest = {
        "bundle_version": ctx.version,
        "built_at": ctx.build_date_iso,
        "catalog_source": ctx.catalog_source,
        "catalog_sha": ctx.catalog_sha,
        "marketplace_name": MARKETPLACE_NAME,
        "plugin_count": len(plugins),
        "plugins": [
            {
                "id": p["id"],
                "name": p["name"],
                "version": p["version"],
                "size_bytes": p["size_bytes"],
            }
            for p in sorted(plugins, key=lambda x: x["id"])
        ],
    }
    if ctx.catalog_bundle_sha:
        manifest["catalog_bundle_sha"] = ctx.catalog_bundle_sha
    out = ctx.bundle_root / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return out


def write_repo_list(plugins: list[dict], ctx: BuildContext) -> Path:
    out = ctx.bundle_root / "repo-list.txt"
    lines = ["marketplace"] + sorted(p["id"] for p in plugins)
    out.write_text("\n".join(lines) + "\n")
    return out


def write_build_summary(ctx: BuildContext, source_count: int) -> Path:
    summary = {
        "version": ctx.version,
        "built_at": ctx.build_date_iso,
        "catalog_source": ctx.catalog_source,
        "catalog_sha": ctx.catalog_sha,
        "catalog_source_count": source_count,
        "included_count": len(ctx.included),
        "failed": ctx.failures,
        "invalid": ctx.invalid,
        "skipped_unverified": ctx.skipped_unverified,
    }
    if ctx.catalog_bundle_sha:
        summary["catalog_bundle_sha"] = ctx.catalog_bundle_sha
    out = ctx.bundle_root / "build-summary.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return out


def copy_bundle_assets(ctx: BuildContext) -> None:
    """Copy import.sh + bundle README(s) into the bundle root."""
    here = Path(__file__).resolve().parent.parent
    import_sh_src = here / "bundle-assets" / "import.sh"
    import_sh_dst = ctx.bundle_root / "import.sh"
    shutil.copyfile(import_sh_src, import_sh_dst)
    import_sh_dst.chmod(0o755)
    # Copy both English and Chinese READMEs.
    # The English one becomes README.md so any tool that opens "the readme" by
    # default finds it; the Chinese one ships alongside.
    shutil.copyfile(here / "bundle-assets" / "README.md", ctx.bundle_root / "README.md")
    zh = here / "bundle-assets" / "README.zh-CN.md"
    if zh.is_file():
        shutil.copyfile(zh, ctx.bundle_root / "README.zh-CN.md")


def make_tarball(ctx: BuildContext) -> Path:
    out = ctx.output_dir / f"costrict-marketplace-bundle-v{ctx.version}.tar.gz"
    # Deterministic tar: fixed mtime, owner/group=0, sorted entries
    epoch_dt = datetime.fromisoformat(ctx.build_date_iso)
    epoch = int(epoch_dt.timestamp())
    arcname_root = ctx.bundle_root.name
    files: list[Path] = []
    for f in ctx.bundle_root.rglob("*"):
        rel = f.relative_to(ctx.bundle_root)
        # Skip intermediate temp dirs
        if rel.parts and rel.parts[0].startswith("_tmp"):
            continue
        files.append(f)
    files.sort(key=lambda p: str(p.relative_to(ctx.bundle_root)))
    # Use a fixed-mtime gzip wrapper so two same-day builds produce byte-identical output.
    with open(out, "wb") as raw, gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=epoch) as gz:
        with tarfile.open(mode="w|", fileobj=gz, format=tarfile.PAX_FORMAT) as tar:
            for f in files:
                arc = f"{arcname_root}/{f.relative_to(ctx.bundle_root).as_posix()}"
                info = tar.gettarinfo(str(f), arcname=arc)
                info.mtime = epoch
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                if info.isfile():
                    with f.open("rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
    return out


def publish_to_github(plugins: list[dict], ctx: BuildContext) -> None:
    """Create + push each bare repo to costrict-plugins-repo org. Idempotent."""
    repos_dir = ctx.bundle_root / "repos" / "plugins"
    for p in sorted(plugins, key=lambda x: x["id"]):
        plugin_id = p["id"]
        bare = repos_dir / f"{plugin_id}.git"
        full = f"{ctx.github_org}/{plugin_id}"
        url = f"https://github.com/{full}.git"
        # Create (swallow 409 already-exists)
        proc = subprocess.run(
            [
                "gh",
                "repo",
                "create",
                full,
                "--public",
                "--disable-issues",
                "--disable-wiki",
                "--description",
                f"costrict mirror of {plugin_id}",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            LOG.error("repo create failed: %s — %s", full, proc.stderr.strip())
            ctx.failures.append({"plugin_id": plugin_id, "reason": f"gh repo create: {proc.stderr.strip()[:200]}"})
            continue
        push = subprocess.run(
            ["git", "push", "--force", "--mirror", url],
            cwd=bare,
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            LOG.error("push failed: %s — %s", full, push.stderr.strip())
            ctx.failures.append({"plugin_id": plugin_id, "reason": f"git push: {push.stderr.strip()[:200]}"})


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the costrict-plugin-marketplace bundle")
    p.add_argument("--version", required=True, help="SemVer for this bundle, e.g. 0.1.0")
    p.add_argument(
        "--catalog",
        type=Path,
        default=Path(os.environ.get("COSTRICT_SKILLS_REPO_PATH", "../costrict-skills-repo")) / "catalog" / "plugins" / "index.json",
        help="Local plugin catalog JSON for manual/debug builds.",
    )
    p.add_argument("--catalog-url", help="Download catalog/plugins/index.json from this URL before building. Debug/backward-compatible path.")
    p.add_argument("--catalog-bundle", type=Path, help="Local upstream catalog-bundle.tar.gz. Preferred public-release input.")
    p.add_argument("--catalog-bundle-url", help="Download upstream catalog-bundle.tar.gz from this URL before building. Preferred public-release input.")
    p.add_argument(
        "--expected-catalog-sha",
        "--catalog-sha",
        dest="expected_catalog_sha",
        help="Expected SHA256 of the JSON catalog file. In bundle mode, kept as an alias for --expected-index-sha.",
    )
    p.add_argument("--expected-bundle-sha", help="Expected SHA256 of catalog-bundle.tar.gz. The build aborts on mismatch.")
    p.add_argument("--expected-index-sha", help="Expected SHA256 of index.json inside catalog-bundle.tar.gz.")
    p.add_argument("--output", type=Path, default=Path("build"))
    p.add_argument("--limit", type=int, default=None, help="Process only first N entries (testing).")
    p.add_argument("--github-org", default=os.environ.get("GITHUB_ORG", GITHUB_ORG_DEFAULT))
    p.add_argument("--publish", action="store_true", help="Push bare repos to GitHub org (requires gh auth).")
    p.add_argument("--workers", type=int, default=8, help="Parallel plugin processors (default: 8).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    if args.catalog_bundle and args.catalog_bundle_url:
        LOG.error("use only one of --catalog-bundle or --catalog-bundle-url")
        return 2
    if (args.catalog_bundle or args.catalog_bundle_url) and args.catalog_url:
        LOG.error("use either catalog bundle input or --catalog-url, not both")
        return 2

    catalog_bundle_sha = None
    source_count = 0

    if args.catalog_bundle or args.catalog_bundle_url:
        catalog_path = args.catalog_bundle
        catalog_source = str(args.catalog_bundle)
        if args.catalog_bundle_url:
            try:
                catalog_path = download_file(args.catalog_bundle_url, args.output / "_catalog", "catalog-bundle.tar.gz")
            except Exception as exc:
                LOG.error("failed to download catalog bundle: %s", exc)
                return 2
            catalog_source = args.catalog_bundle_url
        if catalog_path is None or not catalog_path.is_file():
            LOG.error("catalog bundle not found: %s", catalog_path)
            return 2

        catalog_bundle_sha = catalog_sha(catalog_path)
        if args.expected_bundle_sha:
            expected_bundle_sha = normalize_sha(args.expected_bundle_sha)
            if catalog_bundle_sha != expected_bundle_sha:
                LOG.error("catalog bundle sha mismatch: expected=%s actual=%s source=%s", expected_bundle_sha, catalog_bundle_sha, catalog_source)
                return 2
        try:
            entries, actual_catalog_sha, bundle_manifest, source_count = load_catalog_bundle(catalog_path)
        except Exception as exc:
            LOG.error("failed to read catalog bundle: %s", exc)
            return 2
        expected_index_sha = normalize_sha(args.expected_index_sha or args.expected_catalog_sha or "")
        if expected_index_sha and actual_catalog_sha != expected_index_sha:
            LOG.error("catalog index sha mismatch: expected=%s actual=%s source=%s", expected_index_sha, actual_catalog_sha, catalog_source)
            return 2
        LOG.info(
            "catalog bundle: entries=%d plugins=%d index_sha=%s generated_at=%s",
            source_count,
            len(entries),
            actual_catalog_sha,
            bundle_manifest.get("generated_at", "unknown"),
        )
    else:
        catalog_path = args.catalog
        catalog_source = str(args.catalog)
        if args.catalog_url:
            try:
                catalog_path = download_catalog(args.catalog_url, args.output / "_catalog")
            except Exception as exc:
                LOG.error("failed to download catalog: %s", exc)
                return 2
            catalog_source = args.catalog_url

        if not catalog_path.is_file():
            LOG.error("catalog not found: %s", catalog_path)
            return 2

        actual_catalog_sha = catalog_sha(catalog_path)
        if args.expected_catalog_sha:
            expected_catalog_sha = normalize_sha(args.expected_catalog_sha)
            if actual_catalog_sha != expected_catalog_sha:
                LOG.error("catalog sha mismatch: expected=%s actual=%s source=%s", expected_catalog_sha, actual_catalog_sha, catalog_source)
                return 2
        entries = load_catalog(catalog_path)
        source_count = len(entries)

    LOG.info("catalog entries: %d", len(entries))
    kept, skipped = filter_verified(entries)
    if args.limit:
        kept = kept[: args.limit]
        LOG.info("--limit %d applied; processing %d", args.limit, len(kept))
    today_utc_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    ctx = BuildContext(
        catalog_path=catalog_path,
        catalog_source=catalog_source,
        catalog_sha=actual_catalog_sha,
        catalog_bundle_sha=catalog_bundle_sha,
        output_dir=args.output,
        bundle_root=args.output / f"costrict-marketplace-bundle-v{args.version}",
        cache_dir=args.output / ".cache" / "clones",
        version=args.version,
        build_date_iso=today_utc_midnight.isoformat(),
        plugin_count_limit=args.limit,
        github_org=args.github_org,
        publish=args.publish,
        skipped_unverified=skipped,
    )
    ctx.bundle_root.mkdir(parents=True, exist_ok=True)
    ctx.cache_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("bundle root: %s", ctx.bundle_root)

    workers = max(1, args.workers)
    LOG.info("processing %d plugins with %d parallel workers", len(kept), workers)
    done = 0
    progress_step = max(1, len(kept) // 20)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_plugin, entry, ctx): entry["id"] for entry in kept}
        for fut in as_completed(futures):
            try:
                info = fut.result()
            except Exception as exc:
                pid = futures[fut]
                LOG.exception("unhandled error processing %s", pid)
                with _ctx_lock:
                    ctx.failures.append({"plugin_id": pid, "reason": f"unhandled: {exc}"})
                info = None
            if info is not None:
                with _ctx_lock:
                    ctx.included.append(info)
            done += 1
            if done % progress_step == 0 or done == len(kept):
                LOG.info(
                    "progress: %d/%d (included=%d failed=%d invalid=%d)",
                    done,
                    len(kept),
                    len(ctx.included),
                    len(ctx.failures),
                    len(ctx.invalid),
                )

    LOG.info(
        "build done: included=%d failed=%d invalid=%d skipped_unverified=%d",
        len(ctx.included),
        len(ctx.failures),
        len(ctx.invalid),
        ctx.skipped_unverified,
    )

    if not ctx.included:
        LOG.error("no plugins succeeded; aborting bundle assembly")
        return 3

    write_template(generate_marketplace_template(ctx.included, ctx), ctx)
    write_manifest(ctx.included, ctx)
    write_repo_list(ctx.included, ctx)
    write_build_summary(ctx, source_count=source_count)
    copy_bundle_assets(ctx)
    # Clean up intermediate temp dirs before tar so unpacked dir is also clean.
    for tmp in ("_tmp_commit", "_tmp_work"):
        d = ctx.bundle_root / tmp
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    tarball = make_tarball(ctx)
    LOG.info("bundle written: %s", tarball)

    if args.publish:
        publish_to_github(ctx.included, ctx)
        write_build_summary(ctx, source_count=source_count)  # refresh after publish failures
        if ctx.failures:
            LOG.error("publish completed with %d failures", len(ctx.failures))
            return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
