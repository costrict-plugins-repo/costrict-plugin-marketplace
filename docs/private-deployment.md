# Private Deployment (内网 / 私有化)

**English** · [简体中文](./private-deployment.zh-CN.md)

This is the path for IT admins importing the costrict-plugins marketplace into an air-gapped or restricted corporate network. After import, developers run `csc plugin marketplace add …` against your internal git server and the full plugin catalog is available — no GitHub connectivity needed.

If your workstations can reach github.com directly, you don't need any of this — see [public-usage.md](./public-usage.md) for the one-line path.

---

## Architecture at a glance

```
+--------------------+   1. download   +-------------------+
| github.com         | --------------> | internet jumphost |
| (our releases)     |                 |  (downloads .tar) |
+--------------------+                 +---------+---------+
                                                 | 2. transfer (USB / DMZ)
                                                 v
                                  +--------------+---------+
                                  | extract bundle         |
                                  | run ./import.sh <URL>  |
                                  +-----------+------------+
                                              | 3. git push --mirror x 677
                                              v
                                  +--------------+---------+
                                  | internal git server    |
                                  | (Gitea / GitLab / etc.)|
                                  +-----------+------------+
                                              | 4. csc clone
                                              v
                                  +--------------+---------+
                                  | developer workstation  |
                                  | csc plugin install ... |
                                  +------------------------+
```

The bundle is a single **`.tar.gz`** (≈75 MB for v0.1.0) containing every verified plugin as a bare git repo plus a `marketplace.json` template and the `import.sh` loader. Once `import.sh` pushes the contents to your internal git server, your developers point `csc` at `<your-base-url>/marketplace.git` and operate against your private mirror.

---

## Prerequisites

### Machine that runs `import.sh`

| Requirement | Notes |
| --- | --- |
| `bash` 4.0+ | macOS/Linux/WSL all OK |
| `git` 2.30+ | newer is fine |
| `find`, `xargs`, `sed` | GNU or BSD, both fine |
| Network reach to your internal git server | HTTPS or SSH, doesn't matter |
| Either `git config credential.helper` already set up **or** anonymous push allowed on your server | the script itself never prompts |

### Internal git server

| Requirement | Notes |
| --- | --- |
| Smart-HTTP (or SSH) protocol | i.e. real git server, not a static file mirror. Tested with Gitea ≥ 1.9, GitLab CE/EE, Forgejo, GitHub Enterprise, Bitbucket DC. |
| Either auto-create-on-push **or** an admin willing to pre-create the repos listed in `repo-list.txt` | Most popular servers have a setting for the former |
| ~150-300 MB free disk per import (after first import, deltas only) | |
| Permissive `git push --mirror` (no pre-receive hook that rejects the first commit) | If your server has push rules, allow the `costrict-build` author |

---

## Step-by-step

### 1. Download the bundle on an internet-connected machine

```bash
gh release download v<version> \
    --repo costrict-plugins-repo/costrict-plugin-marketplace \
    --pattern 'costrict-marketplace-bundle-v<version>.tar.gz*'
```

You get two files: `costrict-marketplace-bundle-v<v>.tar.gz` and `.tar.gz.sha256`.

If you can't use `gh`, browse the releases page directly:
<https://github.com/costrict-plugins-repo/costrict-plugin-marketplace/releases>

### 2. Verify the download

```bash
shasum -a 256 -c costrict-marketplace-bundle-v<v>.tar.gz.sha256
```

Both files must verify before proceeding.

### 3. Transfer into your network

Any method works: USB drive, internal file mirror, DMZ scp, signed-package management system, etc. The bundle is intentionally a single file so it travels cleanly through restrictive channels.

### 4. Extract

```bash
tar -xzf costrict-marketplace-bundle-v<v>.tar.gz
cd costrict-marketplace-bundle-v<v>
ls
# manifest.json  marketplace.json.tmpl  import.sh  README.md  repo-list.txt  repos/  build-summary.json
```

### 5. (Optional) Pre-create repos on your git server

**Skip this step if your server auto-creates on push.**

If your git server requires repos to exist before push, the bundle ships `repo-list.txt` — one repo name per line, ~678 entries — that you (or your git admin) can use to batch-create.

For Gitea:
```bash
TOKEN=ghp_xxx  # personal access token with repo create scope
ORG=costrict    # the namespace where you want the plugins to live
while read repo; do
  curl -s -u "admin:$TOKEN" \
       -H 'Content-Type: application/json' \
       -X POST "https://git.internal.corp/api/v1/orgs/$ORG/repos" \
       -d "{\"name\":\"$repo\",\"private\":false}" >/dev/null
done < repo-list.txt
```

For GitLab (need group ID):
```bash
GROUP_ID=42  # your group's numeric id
TOKEN=glpat_xxx
while read repo; do
  curl -s --header "PRIVATE-TOKEN: $TOKEN" \
       "https://gitlab.internal.corp/api/v4/projects?name=$repo&namespace_id=$GROUP_ID&visibility=public" \
       -X POST >/dev/null
done < repo-list.txt
```

For Forgejo: same API as Gitea.

### 6. Run the importer

```bash
./import.sh https://git.internal.corp/costrict
```

The single positional arg is your git server's **base URL** — every plugin gets pushed to `<base>/<plugin-id>.git`, and the marketplace index to `<base>/marketplace.git`. A trailing slash is normalized away.

The run takes **5–15 minutes** on a fast LAN (8 parallel pushes). It's idempotent — re-running with the same args is safe and fast (unchanged plugins skip via git's diff transfer).

Sample output:

```
>> Importing 677 plugin repos to https://git.internal.corp/costrict (parallel=8)
>> Plugin push summary: 677/677 succeeded, 0 failed
>> Rendering & pushing marketplace index to https://git.internal.corp/costrict/marketplace.git
=== Import complete ===
Successfully imported 677/677 plugins.
Marketplace ready at https://git.internal.corp/costrict/marketplace.git

Next steps:
  1. Verify marketplace: csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git
  2. List available plugins: csc plugin list
  3. Install a plugin:       csc plugin install <plugin-name>@costrict-plugins
```

### 7. Wire up `csc` on developer workstations

```bash
csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git
csc plugin list                                # ~677 plugins
csc plugin install <plugin-name>@costrict-plugins
```

That's it. Developers can install any plugin in the marketplace; `csc` fetches each plugin's bare repo from your internal git server on demand.

---

## Updating to a newer bundle

1. Download the new bundle (`v<new>`) on an internet-connected machine
2. Verify, transfer, extract — same as steps 1-4
3. Re-run `./import.sh https://git.internal.corp/costrict` — git's delta transfer means unchanged plugins re-sync as no-ops; only updated/new plugins move bytes
4. `csc plugin marketplace update costrict-plugins` to refresh client-side index

You do **not** need to wipe the previous import.

---

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md) for the complete catalog. The most common issues:

| Symptom | Quick fix |
| --- | --- |
| `fatal: repository '…' not found` during plugin push | Your server doesn't auto-create. Pre-create from `repo-list.txt` (step 5). |
| `error: failed to push some refs … (pre-receive hook declined)` | Server policy rejects first commit. Whitelist the `costrict-build` author. |
| Some plugin pushes time out | LAN saturated; re-run `./import.sh` — push-mirror is idempotent. |
| `csc plugin install` fails after import | Verify the plugin's repo on your server: `git clone https://git.internal.corp/costrict/<plugin-id>.git`. Should clone cleanly with `.claude-plugin/plugin.json` at root. |
| Some repos exist on server but are empty (no refs) | Re-run `./import.sh`; `git push --mirror` is idempotent and will populate them. |

---

## What gets imported

Each plugin's content is **pruned** to runtime essentials before bundling:

**Kept:**
- `.claude-plugin/` (all contents, including `plugin.json`)
- `skills/`, `commands/`, `agents/`, `hooks/` (recursive)
- `LICENSE*` and `README.md` / `README.txt` (top-level)

**Pruned:**
- Source directories: `src/`, `lib/`, `test/`, `tests/`, `spec/`, `examples/`, `demo/`, `docs/`
- Build artifacts: `node_modules/`, `dist/`, `build/`, `target/`, `__pycache__/`
- Media files > 500 KB: `*.png`, `*.jpg`, `*.gif`, `*.mp4`, `*.mov`
- Build configs: `package.json`, `tsconfig.json`, `Cargo.toml`, `pyproject.toml`, etc.

Each plugin's original LICENSE is preserved. The catalog source SHA is recorded in `manifest.json::catalog_sha` for audit purposes.

---

## What customers should NOT do

- **Don't edit individual plugin repos directly on your git server.** They're regenerated from upstream on each bundle release; your changes would be wiped on the next `import.sh` run.
- **Don't delete `marketplace.git` between updates.** It's not "the marketplace" — it's the index file `csc` reads. If you delete it, just re-run `./import.sh` to recreate it.
- **Don't trust `repo-list.txt` as a permanent reference.** It's a per-release artifact. Future releases may add/remove plugins.

For maintainer-side topics (cutting a new release, version bumping, etc.), see [for-maintainers.md](./for-maintainers.md).
