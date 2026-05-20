#!/usr/bin/env bash
# Cut a GitHub Release for the costrict-plugin-marketplace bundle.
#
# Usage: scripts/release.sh <version>
#   <version>: SemVer with NO leading 'v' (e.g. 0.1.0)
#
# Requirements:
#   - `gh` CLI authenticated against costrict-plugins-repo org with `repo` scope
#   - `build/costrict-marketplace-bundle-v<version>.tar.gz` must exist (run build.py first)
#
# Produces:
#   - `<bundle>.sha256` checksum file
#   - GitHub Release `v<version>` with:
#       * the bundle + .sha256 as assets
#       * release notes generated from build-summary.json + diff vs previous release

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <version>" >&2
  echo "Example: $0 0.1.0" >&2
  exit 1
fi

VERSION="$1"
TAG="v$VERSION"
REPO="${RELEASE_REPO:-costrict-plugins-repo/costrict-plugin-marketplace}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="$ROOT/build/costrict-marketplace-bundle-$TAG.tar.gz"
BUNDLE_DIR="$ROOT/build/costrict-marketplace-bundle-$TAG"
SHA_FILE="$BUNDLE.sha256"
SUMMARY="$BUNDLE_DIR/build-summary.json"
MANIFEST="$BUNDLE_DIR/manifest.json"

for bin in gh git python3 shasum; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: required tool not on PATH: $bin" >&2
    exit 1
  fi
done

[[ -f "$BUNDLE" ]] || { echo "ERROR: bundle not found at $BUNDLE; run build.py first" >&2; exit 2; }
[[ -f "$SUMMARY" ]] || { echo "ERROR: build summary not found at $SUMMARY" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest not found at $MANIFEST" >&2; exit 2; }

# 6.5 — refuse to overwrite an existing release.
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "ERROR: release $TAG already exists on $REPO." >&2
  echo "       Releases are immutable; bump the version and rebuild." >&2
  exit 3
fi

# 6.4 — checksum.
echo ">> Generating SHA256 checksum"
( cd "$(dirname "$BUNDLE")" && shasum -a 256 "$(basename "$BUNDLE")" > "$SHA_FILE" )
echo "   $(cat "$SHA_FILE")"

# 6.3 — diff vs previous release.
PREV_TAG="$(gh release list --repo "$REPO" --limit 50 --json tagName --jq '.[].tagName' 2>/dev/null | grep -v "^$TAG\$" | head -1 || true)"

echo ">> Building release notes"
NOTES=$(mktemp)
trap 'rm -f "$NOTES"' EXIT

python3 - <<PY > "$NOTES"
import json, os, sys
manifest = json.load(open(os.environ["MANIFEST"]))
summary = json.load(open(os.environ["SUMMARY"]))
prev_tag = os.environ.get("PREV_TAG") or ""
version = manifest["bundle_version"]

# Source breakdown is not in manifest by design (would couple bundle to catalog source field).
# Pull it from build-summary if present, else show totals only.
print(f"## costrict-plugin-marketplace v{version}")
print()
print(f"- Built at: {manifest['built_at']}")
print(f"- Catalog source: {manifest['catalog_source']}")
print(f"- Catalog SHA: \`{manifest['catalog_sha']}\`")
print(f"- Plugin count: **{manifest['plugin_count']}**")
print()
print("## Build summary")
print()
print(f"- Catalog entries read: {summary['catalog_source_count']}")
print(f"- Successfully bundled: **{summary['included_count']}**")
print(f"- Failed (fetch/prune/init errors): {len(summary['failed'])}")
print(f"- Invalid (missing .claude-plugin/plugin.json after prune): {len(summary['invalid'])}")
print(f"- Skipped (marketplace_verified=false): {summary['skipped_unverified']}")
print()

# Top 10 largest
top = sorted(manifest['plugins'], key=lambda p: p['size_bytes'], reverse=True)[:10]
print("## Top 10 largest plugins")
print()
print("| # | Plugin | Size (KB) |")
print("|---|--------|-----------|")
for i, p in enumerate(top, 1):
    print(f"| {i} | \`{p['id']}\` | {p['size_bytes']/1024:.1f} |")
print()

if prev_tag:
    print(f"## Changes since {prev_tag}")
    print()
    # gh release download to a tmp dir to load prev manifest
    import subprocess, tempfile, shutil
    tmpdir = tempfile.mkdtemp()
    try:
        subprocess.run(
            ["gh", "release", "download", prev_tag,
             "--repo", os.environ["REPO"],
             "--pattern", "costrict-marketplace-bundle-*.tar.gz",
             "--dir", tmpdir],
            check=False, capture_output=True,
        )
        # find the tarball and extract manifest
        import glob, tarfile as tf
        archives = glob.glob(os.path.join(tmpdir, "*.tar.gz"))
        prev_plugins = None
        if archives:
            with tf.open(archives[0], "r:gz") as t:
                for m in t.getmembers():
                    if m.name.endswith("/manifest.json"):
                        f = t.extractfile(m)
                        prev_plugins = {p["id"]: p for p in json.load(f)["plugins"]}
                        break
        if prev_plugins is None:
            print(f"_(Could not load manifest from {prev_tag}; diff omitted.)_")
        else:
            cur = {p["id"]: p for p in manifest["plugins"]}
            added = sorted(set(cur) - set(prev_plugins))
            removed = sorted(set(prev_plugins) - set(cur))
            updated = sorted([i for i in set(cur) & set(prev_plugins) if cur[i]["version"] != prev_plugins[i]["version"]])
            def block(label, items):
                print(f"### {label} ({len(items)})")
                print()
                if not items:
                    print("_(none)_")
                else:
                    for i in items[:50]:
                        print(f"- \`{i}\`")
                    if len(items) > 50:
                        print(f"- … (+{len(items) - 50} more)")
                print()
            block("Added", added)
            block("Removed", removed)
            block("Version-updated", updated)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
else:
    print("## Initial release")
    print()
    print("No previous release exists; this is the first published bundle.")

print()
print("## Installation")
print()
print("1. Download both the bundle and \`.sha256\` file.")
print(f"2. Verify: \`shasum -a 256 -c costrict-marketplace-bundle-{prev_tag or 'v'+version}.tar.gz.sha256\`")
print(f"3. Extract: \`tar -xzf costrict-marketplace-bundle-v{version}.tar.gz && cd costrict-marketplace-bundle-v{version}\`")
print("4. Import to your internal git server: \`./import.sh <your-git-base-url>\`")
print()
PY

echo ">> Release notes preview (first 30 lines):"
head -30 "$NOTES"
echo "..."
echo

# 6.1 — cut the release.
echo ">> Creating GitHub release $TAG on $REPO"
gh release create "$TAG" \
  --repo "$REPO" \
  --title "costrict-plugin-marketplace $TAG" \
  --notes-file "$NOTES" \
  "$BUNDLE" \
  "$SHA_FILE"

echo
echo "=== Release complete ==="
echo "View: https://github.com/$REPO/releases/tag/$TAG"
