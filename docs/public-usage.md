# Public Usage (公网用户)

**English** · [简体中文](./public-usage.zh-CN.md)

If your `csc` workstation can reach `github.com`, you do **not** need the bundle, `import.sh`, or any private deployment work. Our GitHub-hosted marketplace works just like Anthropic's `claude-plugins-official` — one command, no download.

## Setup

```bash
csc plugin marketplace add https://github.com/costrict-plugins-repo/marketplace.git
```

`csc` clones the marketplace index (a few KB) and registers it under the name `costrict-plugins`.

## Browse & install

```bash
# list everything available
csc plugin list

# search by keyword
csc plugin list | grep -i security

# install a plugin (csc fetches just this plugin's repo on demand)
csc plugin install <plugin-name>@costrict-plugins
```

`csc` clones each plugin's bare git repo from `https://github.com/costrict-plugins-repo/<plugin-id>.git` only when you install it — the marketplace index itself stays tiny.

## Update

```bash
# refresh marketplace index (catches new + removed plugins)
csc plugin marketplace update costrict-plugins

# re-install a plugin to pick up new version
csc plugin install <plugin-name>@costrict-plugins --force
```

## Remove

```bash
csc plugin uninstall <plugin-name>@costrict-plugins
csc plugin marketplace remove costrict-plugins
```

## What you'll see

| Item | Where |
| --- | --- |
| Marketplace listing | `https://github.com/costrict-plugins-repo/marketplace` |
| Individual plugin repo | `https://github.com/costrict-plugins-repo/<plugin-id>` |
| All plugins (org page) | `https://github.com/orgs/costrict-plugins-repo/repositories` |

Each plugin's own LICENSE and README live inside its repo — `csc` extracts them on install. We don't add or alter any plugin's source; we mirror the verified subset of [`costrict-skills-repo`](https://github.com/zgsm-ai/everything-ai-coding)'s plugin catalog and prune to the minimum required for runtime (`.claude-plugin/`, `skills/`, `commands/`, `agents/`, `hooks/`, `LICENSE`, `README.md`).

## FAQ

**Q: Is this different from Anthropic's `claude-plugins-official` marketplace?**

Yes — we mirror a much larger set (~770 verified plugins from 3 upstream sources: `claude-plugins-official`, `superpowers-marketplace`, and `claude-plugins-dev`). Anthropic's official marketplace contains only the ~178 plugins they directly review.

**Q: Can I add a plugin to your marketplace?**

We're a mirror of the [`costrict-skills-repo`](https://github.com/zgsm-ai/everything-ai-coding) catalog — open an issue / PR there. Our build pipeline picks it up on the next release.

**Q: How fresh is the data?**

Each `costrict-marketplace-bundle-v<ver>` release is a snapshot. The release notes show the catalog SHA the build was made from. For continuous updates you'd want `csc plugin marketplace update` against the latest catalog (no release cycle).

**Q: What if a plugin install fails with "404" or similar?**

That plugin's upstream repo may have moved or become private. File a bug at our [marketplace repo](https://github.com/costrict-plugins-repo/costrict-plugin-marketplace/issues) — we'll either patch the catalog or remove the broken entry on the next build.

**Q: My company blocks `github.com` — what do I do?**

You want the private-deployment path. See [docs/private-deployment.md](./private-deployment.md).
