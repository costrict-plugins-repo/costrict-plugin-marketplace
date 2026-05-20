# For Maintainers: cutting a new costrict-plugin-marketplace release

**English** · [简体中文](./for-maintainers.zh-CN.md)

This is the standard operating procedure for whoever builds and publishes new bundles. The pipeline is intentionally manual in v0.x — there is no scheduled CI; every release is a deliberate human action.

## One-time setup

```bash
git clone git@github.com:costrict-plugins-repo/costrict-plugin-marketplace.git
cd costrict-plugin-marketplace

# Configure environment.
cp .env.example .env
$EDITOR .env   # fill GITHUB_TOKEN, COSTRICT_SKILLS_REPO_PATH, GITHUB_ORG

# Authenticate gh CLI against the same account/org as your PAT.
gh auth login
gh auth status

# Sanity-check that the catalog is reachable.
ls "$COSTRICT_SKILLS_REPO_PATH/catalog/plugins/index.json"
```

The PAT needs scopes:
- `repo` (create + push to plugin mirrors under `costrict-plugins-repo`)
- `workflow` (only if/when CI is added later)

## Building a release

```bash
# 1. Pick a version. See "Version bumping" below.
VERSION=0.2.0

# 2. Build the bundle locally (no GitHub side-effects).
python3 scripts/build.py --version "$VERSION" --output build

# 3. Inspect the build summary.
jq '.included_count, .failed | length, .invalid | length, .skipped_unverified' \
  build/costrict-marketplace-bundle-v$VERSION/build-summary.json
# Eyeball failed/invalid for anything systemic. Use the per-plugin reason
# to decide whether to fix PRUNE_PATTERNS, the fetch heuristic, or accept the loss.

# 4. (Optional) sanity-check a few plugins.
for id in $(jq -r '.plugins[0:5][].id' build/costrict-marketplace-bundle-v$VERSION/manifest.json); do
  rm -rf /tmp/verify-$id
  git clone build/costrict-marketplace-bundle-v$VERSION/repos/plugins/$id.git /tmp/verify-$id
  ls /tmp/verify-$id/.claude-plugin/plugin.json
done

# 5. Push the bare repos to the GitHub org (idempotent, throttled, retries empty repos).
./scripts/publish.sh build/costrict-marketplace-bundle-v$VERSION

# 6. Cut the GitHub Release.
scripts/release.sh "$VERSION"

# 7. Verify externally.
gh release view "v$VERSION" --repo costrict-plugins-repo/costrict-plugin-marketplace
```

## Smoke-testing import.sh before publishing

When you change `import.sh` itself, run a local end-to-end:

```bash
# Pre-create empty bare repos under /tmp/git-srv/.
mkdir -p /tmp/git-srv && for r in $(cat build/costrict-marketplace-bundle-v$VERSION/repo-list.txt); do
  git init --bare --quiet /tmp/git-srv/$r.git
  git -C /tmp/git-srv/$r.git config http.receivepack true
done

# Start the bundled smart-HTTP server.
python3 scripts/git-smart-http.py --root /tmp/git-srv --port 8848 &

# Run import.sh from the extracted bundle.
cd build/costrict-marketplace-bundle-v$VERSION && ./import.sh http://127.0.0.1:8848

# Confirm marketplace is clonable.
git clone http://127.0.0.1:8848/marketplace.git /tmp/verify-mp
cat /tmp/verify-mp/.claude-plugin/marketplace.json | jq '.plugins | length'

# Tear down.
kill %1 && rm -rf /tmp/git-srv /tmp/verify-mp
```

## Version bumping

We follow SemVer per the `marketplace-publish` spec:

| Change | Bump |
| --- | --- |
| `import.sh` / docs / build pipeline fixes only, no plugin set change | PATCH (`v0.1.0` → `v0.1.1`) |
| Plugins added or removed, no breaking format change | MINOR (`v0.1.0` → `v0.2.0`) |
| Bundle layout, manifest schema, or marketplace.json schema change | MAJOR (`v0.x` → `v1.0.0`) |

The first stable release after several customer deployments may be cut directly to `v1.0.0`.

## Releases are immutable

We never overwrite or delete a published release. If a release is broken, mark it as "pre-release" in the GitHub UI and cut a new one (PATCH or MINOR depending on severity). Customers may have downloaded the broken bundle and removing it could break audit trails.

## Publishing to GitHub

Use `scripts/publish.sh` (not `build.py --publish` — that exists but is unmaintained; `publish.sh` has the throttling + empty-repo repair logic):

```bash
./scripts/publish.sh build/costrict-marketplace-bundle-v$VERSION
```

Key behaviors:

- **Two phases**: serial repo create (with 2s gap + exponential backoff on rate limit) then parallel `git push --mirror` (4 concurrent).
- **Idempotent**: re-running with `--skip-existing` skips repos already on GitHub with content. Empty-but-created repos are auto-detected and pushed in the same run.
- **Hits the GitHub secondary rate limit at ~138 burst creates.** When it does, the script sleeps 120s × N (up to 8 retries). If those exhaust, just rerun later — `--skip-existing` keeps progress.
- **Never run two `publish.sh` instances in parallel** — they'll both hit rate limits and prolong the block.

## Troubleshooting

For the full catalog see [troubleshooting.md](./troubleshooting.md). Quick hits:

**GitHub secondary rate limit during publish** — Re-run with `--skip-existing` after a 10–15 min cooldown.

**Some plugins in the org are empty (no refs)** — A bug we fixed but worth knowing: kill the running `publish.sh`, re-run with `--skip-existing` — the script probes for empty repos at startup and includes them in the push set.

**One plugin keeps failing fetch** — Its `source_url` may be 404 or moved. Check `build-summary.json::failed`. Either fix the catalog entry upstream in `costrict-skills-repo` or accept the gap until next refresh.

**Reproducibility broke** — Two same-day builds should produce identical md5. If not, a non-zero mtime is leaking into the tar. Check `make_tarball` in `scripts/build.py` — every `info.mtime` and the `gzip.GzipFile(mtime=…)` must be pinned to `ctx.build_date_iso`.

**Build runs but workers=1 (slow)** — Old version of `build.py` without `ThreadPoolExecutor`. Pull latest.

## Project boundaries

This project is intentionally independent of `costrict-web`. It does not touch:
- The Go server or its endpoints
- The `capability_items` table or any DB
- The `/hub` favorite API

Plugin metadata enrichment for `/hub` is the scope of `add-plugin-capability-type`; the two changes are decoupled.
