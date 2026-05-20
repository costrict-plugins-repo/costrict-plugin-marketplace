# Troubleshooting

**English** · [简体中文](./troubleshooting.zh-CN.md)

Real failure modes observed during build, publish, and customer import — with fixes.

## Customer-side: running `import.sh`

### `fatal: repository '<url>' not found` during push

Your git server doesn't auto-create repos on push.

**Fix:** Pre-create the repos listed in `repo-list.txt` (one per line) via your git server's API. See [private-deployment.md §5](./private-deployment.md#5-optional-pre-create-repos-on-your-git-server) for Gitea/GitLab/Forgejo recipes.

### `error: failed to push some refs … (pre-receive hook declined)`

Your server has a push policy (signed commits, GPG-required, branch-protection-on-empty-repo, etc.) that rejects the first push.

**Fix:** Whitelist the `costrict-build <build@costrict.local>` author / `main` branch for the costrict namespace. Or temporarily disable the offending rule for the import run.

### Many plugins fail with `Connection reset by peer` / `HTTP 500`

The git server (especially smaller ones like raw `git-http-backend` CGI) can choke under 8 parallel pushes with chunked-transfer bodies. Symptom: a handful of larger plugins fail while smaller ones succeed.

**Fix:** Re-run `./import.sh` — push-mirror is idempotent and the failed ones will retry. Or lower parallelism by editing the `-P 8` in `import.sh` to `-P 4`.

### `Everything up-to-date` but plugin shows as failed

Some git servers return HTTP 500 mid-push, then on retry git client prints `Everything up-to-date` because some refs landed before the failure. The script treats non-zero exit as failure — actually the repo may still be partially populated.

**Fix:** Re-run `./import.sh`. The second run will detect mismatched refs and force-push the missing content (`--mirror` semantics).

### Slow imports on a fast LAN

Default parallelism is 8 concurrent pushes. Bottleneck is usually the git server's `receive-pack` throughput.

**Fix:** Edit `import.sh` and bump `-P 8` to `-P 16` (or higher). Watch the server's CPU/IO to make sure you don't overload it.

### Network proxy interfering

If your machine has `HTTPS_PROXY` / `HTTP_PROXY` set, git inherits them. For internal-only target servers this often breaks.

**Fix:** Add your internal git host to `no_proxy`:
```bash
export no_proxy=git.internal.corp,$no_proxy
./import.sh https://git.internal.corp/costrict
```

### `csc plugin install` fails after a successful import

Most likely a `csc` setup issue, not an import issue. Verify the plugin repo on your server first:
```bash
git clone https://git.internal.corp/costrict/<plugin-id>.git /tmp/verify
ls /tmp/verify/.claude-plugin/plugin.json  # must exist
```

If that clone works and `.claude-plugin/plugin.json` is there, `csc` should be able to install it. Common csc-side issues:
- `csc plugin marketplace list` doesn't show `costrict-plugins` — re-add the marketplace
- `csc plugin marketplace update costrict-plugins` after the import to refresh
- Verify `~/.claude/settings.json` has the marketplace entry

### Some plugin repos exist on the server but are empty (`0 refs`)

This shouldn't happen on a clean `import.sh` run, but can occur if the script was interrupted between push attempts.

**Fix:** Re-run `./import.sh` — the script pushes to every plugin in the bundle each invocation; existing-but-empty repos get populated on the next run.

---

## Maintainer-side: building bundles

### `WARNING fetch failed: <plugin> (clone timed out)`

A plugin's upstream repo is genuinely slow (large LFS, slow region, network blip).

**Fix:** Either:
- Bump `CLONE_TIMEOUT_SECONDS` in `scripts/build.py` (default 90s) and re-run — the build is resumable (existing bare repos skip)
- Or accept the loss and document it in the release notes; the catalog will likely fix itself on the next upstream sync

### `WARNING plugin.json not found: <plugin>`

The upstream repo doesn't have `.claude-plugin/plugin.json` where we expect it. Either the plugin author hasn't packaged it as a marketplace plugin, or the catalog's `marketplace_repo` / `plugin_name` is wrong.

**Fix:** Inspect the upstream by hand. If it really is a misclassification, file an issue at `costrict-skills-repo` to fix the catalog entry.

### Reproducibility broken: two same-day builds produce different `md5`

Most likely you introduced a non-zero mtime somewhere in the bundle. Check:
- `manifest.json::built_at` is set to `ctx.build_date_iso` (UTC midnight)
- `build-summary.json::built_at` same
- bare repo commit env has `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` pinned
- `make_tarball` uses `gzip.GzipFile(mtime=epoch, …)` and `info.mtime = epoch` for every tar entry

Detect drift by diffing two extracted bundles — usually 1 or 2 files reveal the culprit.

### Build is sequential and slow

Default `--workers 8` should run fetch in parallel. Confirm with:
```bash
python3 scripts/build.py --version <v> --workers 8 -v
```

If workers=1, you may have an old version of `build.py` without the `ThreadPoolExecutor` patch.

---

## Maintainer-side: pushing to GitHub (`publish.sh`)

### `GraphQL: You have created too many repositories, too quickly`

GitHub's secondary rate limit for content creation. Triggered by burst creation (~138+ repos in a few minutes).

**Fix:** The script auto-backs off (`120s × N` retries, max 8). If it gives up: stop all `gh` traffic for 10-15 minutes, then re-run with `--skip-existing`. Avoid running multiple `publish.sh` instances in parallel — that *prolongs* the rate limit.

### `Get "https://api.github.com/...": net/http: TLS handshake timeout`

Transient network blip. The script counts these as CREATE_FAILs but doesn't auto-retry within the same run.

**Fix:** Re-run `publish.sh` with `--skip-existing` — the patched script detects empty-but-created repos and pushes content to them (in addition to creating the still-missing ones).

### After publish, some repos exist but are empty

This was a real bug (fixed in `publish.sh` as of v0.1.0). Symptom: `gh api orgs/<org>/repos` shows 200+ repos but `git ls-remote` returns 0 refs for many of them.

**Cause:** Phase 1 of `publish.sh` creates repos serially; Phase 2 pushes content to them in parallel. If the script is killed between phases, the created-but-not-pushed repos sit empty. On the next run with `--skip-existing`, the old logic skipped them entirely.

**Fix:** The current `publish.sh` probes for empty repos at startup (parallel `git ls-remote`) and includes them in the push set regardless of `--skip-existing`. Just re-run the script.

### Want to nuke + retry from scratch

Don't delete repos one at a time. Use a batch script:
```bash
gh api orgs/costrict-plugins-repo/repos --paginate --jq '.[].name' | while read repo; do
  gh repo delete "costrict-plugins-repo/$repo" --yes
done
```

(Or skip the loop and use `gh repo delete` for individual ones.)

**Caveat:** GitHub may also rate-limit DELETE operations.

---

## Smart-HTTP server (`scripts/git-smart-http.py`)

Only matters if you're using our test server locally to simulate a customer deployment. Real customer git servers (Gitea/GitLab/etc.) handle all of these correctly.

### Push of large plugin fails with `HTTP 500` / `Connection reset`

The minimal smart-HTTP server didn't support HTTP `Transfer-Encoding: chunked`, which `git push` uses for pack payloads larger than ~1 MB. **Fixed** as of v0.1.0 — handler now decodes chunked bodies before forwarding to `git-http-backend` and sets `CONTENT_LENGTH` from the decoded size.

If you hit this on an older build, update `scripts/git-smart-http.py` to the latest version.

### `ERROR: could not locate git-http-backend on this system`

The CGI binary isn't on a standard path. The script tries common locations + `git --exec-path`. If those all fail:

```bash
find / -name 'git-http-backend' 2>/dev/null   # locate manually
# then edit scripts/git-smart-http.py and add the path to GIT_HTTP_BACKEND_CANDIDATES
```

### Concurrent pushes occasionally drop

The script uses `ThreadingHTTPServer` and one `git-http-backend` subprocess per request. Under 8+ concurrent pushes of large repos, the OS may queue some connections and the client sees transient drops.

**Fix:** Re-run the customer-side `./import.sh` (idempotent) until the success count hits 100%.

---

## Catalog issues

These are upstream — file at <https://github.com/zgsm-ai/everything-ai-coding/issues>, not this repo.

### A plugin's source repo is 404 or moved

The upstream sync script in `costrict-skills-repo` should catch this on the next refresh. Until then, the entry will fail every build with `fetch: git error 128`.

### Plugin's `marketplace_verified` is `true` but content is broken

The verified flag is set heuristically. If a plugin builds successfully but `csc plugin install` fails or the plugin doesn't work, the catalog entry needs review.

---

## Where to file what

| Issue scope | File at |
| --- | --- |
| `import.sh` / bundle format / publish.sh | `costrict-plugins-repo/costrict-plugin-marketplace` |
| `csc` CLI behavior | csc repo (separate project) |
| A specific plugin's content / availability | `costrict-skills-repo` (upstream catalog) |
| The plugin author's own repo | upstream `<owner>/<plugin>` directly |
