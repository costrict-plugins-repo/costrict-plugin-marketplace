# costrict-marketplace-bundle

**English** · [简体中文](./README.zh-CN.md)

This bundle delivers the full `costrict-plugins` plugin catalog (mirrored from upstream public sources) as bare git repos, plus an `import.sh` that loads them into your internal git server. After import, point `csc` at your git server's marketplace URL and the full plugin set becomes available behind your firewall.

For a fuller treatment — architecture, prerequisites, server compatibility matrix, troubleshooting — see the [costrict-plugin-marketplace project docs](https://github.com/costrict-plugins-repo/costrict-plugin-marketplace/tree/main/docs).

## Bundle contents

| Path | Purpose |
| --- | --- |
| `manifest.json` | bundle version, build timestamp, catalog provenance, list of every plugin (id, name, version, size). |
| `marketplace.json.tmpl` | `csc`-compatible marketplace index; contains `{{BASE_URL}}` placeholders that `import.sh` substitutes. |
| `repos/plugins/<id>.git/` | one bare git repo per plugin, ready to `git push --mirror` to your server. |
| `repo-list.txt` | line-delimited list of every repo this bundle expects (`marketplace` + plugin IDs). Hand to your git admin if your server can't auto-create repos on push. |
| `import.sh` | the loader you run once against your internal git URL. |
| `build-summary.json` | counts of succeeded / failed / invalid / skipped plugins from the build run. |

## Prerequisites

On the machine that runs `import.sh`:

- `bash` 4.0+
- `git` 2.30+
- `find`, `xargs`, `sed` (GNU or BSD, both fine)
- Network reachability to your internal git server
- Either your git server accepts anonymous push, **or** you've configured `git config credential.helper` so push works without prompting

On your git server (one of):

- Auto-create-on-push enabled (Gitea, GitLab, Forgejo all support this)
- **Or** an admin pre-creates the repos listed in `repo-list.txt` first

## Usage

```bash
# 1) extract
tar -xzf costrict-marketplace-bundle-v<version>.tar.gz
cd costrict-marketplace-bundle-v<version>/

# 2) import (runs ~5–15 minutes on a local LAN; 8 parallel pushes)
./import.sh https://git.internal.corp/costrict

# 3) tell csc about the new marketplace
csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git

# 4) verify
csc plugin list
csc plugin install <plugin-name>@costrict-plugins
```

The single positional argument to `import.sh` is your git server's **base URL**: every plugin is pushed to `<base>/<plugin-id>.git`, and the marketplace index is pushed to `<base>/marketplace.git`. A trailing slash is stripped automatically.

## Pre-create repos (when needed)

If your git server can't auto-create on push, the `repo-list.txt` in this bundle lists every repo name `import.sh` will push to. Hand it to your git admin.

### Gitea / Forgejo

```bash
TOKEN=ghp_xxx; ORG=costrict
while read repo; do
  curl -s -u "admin:$TOKEN" \
       -H 'Content-Type: application/json' \
       -X POST "https://git.internal.corp/api/v1/orgs/$ORG/repos" \
       -d "{\"name\":\"$repo\",\"private\":false}" >/dev/null
done < repo-list.txt
```

### GitLab

```bash
GROUP_ID=42; TOKEN=glpat_xxx
while read repo; do
  curl -s --header "PRIVATE-TOKEN: $TOKEN" \
       "https://gitlab.internal.corp/api/v4/projects?name=$repo&namespace_id=$GROUP_ID&visibility=public" \
       -X POST >/dev/null
done < repo-list.txt
```

## Troubleshooting

**`fatal: repository '…' not found`** — your git server didn't auto-create. Pre-create from `repo-list.txt` (see above).

**`error: failed to push some refs … (pre-receive hook declined)`** — server policy blocks the push. Either relax the rule for the `costrict-build <build@costrict.local>` author, or pre-create the repos so the first commit lands without policy evaluation.

**A handful of pushes fail under load** — concurrent `git push` over HTTP can stress smaller servers. `import.sh` continues past failures and reports them at the end. To retry only the failures, re-run `./import.sh` with the same args — `git push --mirror` is idempotent.

**Slow import** — `import.sh` runs `-P 8` parallel pushes by default. To go faster on a strong LAN, edit the script and bump it. To reduce server load, lower it.

**Some repos exist on the server but show "empty repository" when cloned** — re-run `./import.sh`; idempotent re-push will populate them.

**My corporate proxy is interfering** — add the internal git host to `no_proxy`:
```bash
export no_proxy=git.internal.corp,$no_proxy
./import.sh https://git.internal.corp/costrict
```

For the full troubleshooting catalog, see the project docs link at the top of this file.

## Verification

After `./import.sh` finishes:

1. `csc plugin marketplace add <base-url>/marketplace.git` — should report the new marketplace
2. `csc plugin marketplace list` — `costrict-plugins` should appear
3. `csc plugin list` — should show roughly the count in `manifest.json::plugin_count`
4. Install one plugin: `csc plugin install <pick-one>@costrict-plugins`

If all four pass, the round-trip works.

## Updating

When a new bundle is released, repeat the same flow. `git push --mirror` is idempotent — unchanged plugins won't re-transfer content; only the changed ones move bytes. You do **not** need to wipe the previous import.

## License & provenance

This bundle is a mirror of public, third-party plugins. Each plugin's original LICENSE is preserved inside its bare repo. See `manifest.json::catalog_source` for the source catalog and `catalog_sha` for the exact upstream snapshot this bundle was built from.
