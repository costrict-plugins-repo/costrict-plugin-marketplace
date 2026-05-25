# costrict-plugin-marketplace

**English** · [简体中文](./README.zh-CN.md)

A mirror of the upstream `costrict-skills-repo` plugin catalog, usable two ways:

1. **公网 / Public** — point `csc` directly at our GitHub-hosted marketplace; one command, no download.
2. **私有化 / Private** — download a single `.tar.gz` bundle, run `import.sh` against an internal git server, and the entire plugin catalog becomes available behind your firewall without any GitHub access.

Both modes resolve to the same `marketplace.json` schema and the same per-plugin bare git repos, so the `csc` user experience is identical regardless of which side a developer comes from.

## Quick start

### 公网用户 (you have internet access)

```bash
csc plugin marketplace add https://github.com/costrict-plugins-repo/marketplace.git
csc plugin list                                  # ~770 verified plugins
csc plugin install <plugin-name>@costrict-plugins
```

That's it — see [docs/public-usage.md](docs/public-usage.md) for verification steps and FAQ.

### 私有化客户 (internal / air-gapped network)

```bash
# 1. (on a machine with internet) download bundle + checksum
gh release download v0.1.0 \
    --repo costrict-plugins-repo/costrict-plugin-marketplace \
    --pattern 'costrict-marketplace-bundle-v0.1.0.tar.gz*'

# 2. verify
shasum -a 256 -c costrict-marketplace-bundle-v0.1.0.tar.gz.sha256

# 3. transfer the tarball into your isolated network (USB / DMZ / internal mirror)
#    extract it on a machine that can reach your internal git server:
tar -xzf costrict-marketplace-bundle-v0.1.0.tar.gz
cd costrict-marketplace-bundle-v0.1.0

# 4. push every plugin + marketplace to your git server
./import.sh https://git.internal.corp/costrict

# 5. wire up csc on developer workstations
csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git
csc plugin list
```

Full SOP, prerequisites, server compatibility matrix, troubleshooting: [docs/private-deployment.md](docs/private-deployment.md).

## What's in the bundle

Every release exports a single `costrict-marketplace-bundle-v<semver>.tar.gz` with this shape:

```
costrict-marketplace-bundle-v<ver>/
├── manifest.json              # bundle metadata + plugin list (sorted, with sizes)
├── marketplace.json.tmpl      # csc-compatible index; {{BASE_URL}} substituted by import.sh
├── repos/plugins/<id>.git/    # one bare git repo per plugin (~677 in v0.1.0)
├── repo-list.txt              # all repo names; hand to git admin for pre-create
├── import.sh                  # customer-side loader (bash 4+, no extra deps)
├── README.md                  # bundle-local quickstart + troubleshooting
└── build-summary.json         # build statistics (counts, failures, invalid)
```

The bundle is **reproducible**: two same-day builds against the same catalog produce byte-identical tar.gz.

## Repo layout

```
costrict-plugin-marketplace/
├── scripts/
│   ├── build.py               # fetch + prune + bare-repo + bundle pipeline
│   ├── publish.sh             # push bundle to GitHub org (throttled, idempotent)
│   ├── release.sh             # gh CLI release helper (cut tag + upload)
│   └── git-smart-http.py      # local smart-HTTP git server for testing
├── bundle-assets/
│   ├── import.sh              # copied verbatim into every bundle
│   ├── README.md              # copied into bundle as README.md
│   └── README.zh-CN.md        # copied into bundle as README.zh-CN.md
├── docs/
│   ├── public-usage.md        # for csc users on the open internet
│   ├── private-deployment.md  # SOP for customer admins importing the bundle
│   ├── for-maintainers.md     # how to build + publish + cut releases
│   ├── troubleshooting.md     # combined error catalog
│   └── *.zh-CN.md             # Chinese counterparts of each doc above
├── LICENSE                    # MIT
└── README.md / README.zh-CN.md
```

## Naming conventions

| Thing | Value |
| --- | --- |
| GitHub org | `costrict-plugins-repo` |
| Plugin repos | `costrict-plugins-repo/<plugin-id>` (flat, no namespace prefix) |
| Marketplace index repo | `costrict-plugins-repo/marketplace` |
| Build pipeline repo | `costrict-plugins-repo/costrict-plugin-marketplace` (this repo) |
| `marketplace.json::name` | `costrict-plugins` |
| Release tag | `v<semver>` |
| Bundle filename | `costrict-marketplace-bundle-v<semver>.tar.gz` |

## Relationship with `costrict-web`

This project does **not** touch:

- The `costrict-web` Go server
- The `capability_items` table or any database
- The `/hub` favorite API

The codebases stay separate, but public publishing is coordinated by the pinned upstream `catalog-bundle.tar.gz` and its embedded index SHA. The mirror must be built from the same upstream catalog artifact that `costrict-web` ingests so web-visible plugins are installable from `costrict-plugins`.

## License

[MIT](./LICENSE)
