# 维护者指南 (For Maintainers)

[English](./for-maintainers.md) · **简体中文**

本文是给负责出 bundle 的人看的 SOP。

公共 mirror 必须使用和 `costrict-web` ingest 完全相同的上游 `catalog-bundle.tar.gz` 构件构建。交接契约是 `catalog_bundle_url + bundle_sha + index_sha`；构建时会同时校验 bundle 本身和内部 `index.json` 的 SHA，不匹配就直接失败。这样可以避免 web 已经展示 / 收藏某个 plugin，但 `costrict-plugins` mirror 还没有发布它。

## 一次性环境配置

```bash
git clone git@github.com:costrict-plugins-repo/costrict-plugin-marketplace.git
cd costrict-plugin-marketplace

# 配 .env
cp .env.example .env
$EDITOR .env   # 填 GITHUB_TOKEN, COSTRICT_SKILLS_REPO_PATH, GITHUB_ORG

# 认证 gh CLI（账号 / org 跟 PAT 对应）
gh auth login
gh auth status

# 手动 / debug 构建时检查本地 catalog 可达
ls "$COSTRICT_SKILLS_REPO_PATH/catalog/plugins/index.json"
```

PAT 需要的 scope：
- `repo`（创建 + 推送到 `costrict-plugins-repo` 下的 plugin mirror）
- `workflow`（触发 publish workflow）

如果 `catalog_bundle_url` 指向私有 GitHub URL，workflow 会用 `MARKETPLACE_GITHUB_TOKEN` 作为 `CATALOG_DOWNLOAD_TOKEN` 下载 catalog bundle。

## 推荐的 CI 发版路径

上游 catalog release workflow 产出 catalog 构件后，应通过 `workflow_call` 调用 `.github/workflows/publish-marketplace.yml`。紧急 / 手动场景下，也可以用同一个 workflow 和固定 catalog release 构件触发：

```bash
VERSION=0.2.0
CATALOG_BUNDLE_URL=https://github.com/costrict-skills-repo/costrict-skills-repo/releases/download/catalog-bundle-v2026-05-21/catalog-bundle.tar.gz
BUNDLE_SHA=<sha256-of-catalog-bundle-tar-gz>
INDEX_SHA=<sha256-of-index-json-inside-bundle>

gh workflow run publish-marketplace.yml \
  --repo costrict-plugins-repo/costrict-plugin-marketplace \
  -f catalog_bundle_url="$CATALOG_BUNDLE_URL" \
  -f bundle_sha="$BUNDLE_SHA" \
  -f index_sha="$INDEX_SHA" \
  -f version="$VERSION" \
  -f publish=true \
  -f create_release=false
```

workflow 会：

1. 下载 `catalog_bundle_url`。
2. 用 `bundle_sha` 校验 tarball。
3. 用 `index_sha` 校验内部 `index.json`。
4. 构建 bundle，并在 `manifest.json` 中记录 `catalog_source`、`catalog_sha`（即 `index_sha`）和 `catalog_bundle_sha`。
5. 用 `publish.sh --skip-existing --yes` 发布 plugin repos 和 `costrict-plugins` 的 marketplace.git。
6. 上传 bundle 作为 workflow artifact。

只有需要在同一轮切 GitHub Release asset 时，才把 `create_release=true`。

公共发布不要再从一个会移动的本地 checkout 重建。使用本地 catalog 构建只适合 debug。

## 手动 / debug 发版路径

```bash
# 1. 选版本号。见下面的"版本号策略"
VERSION=0.2.0
CATALOG_BUNDLE_URL=https://github.com/costrict-skills-repo/costrict-skills-repo/releases/download/catalog-bundle-v2026-05-21/catalog-bundle.tar.gz
BUNDLE_SHA=<sha256-of-catalog-bundle-tar-gz>
INDEX_SHA=<sha256-of-index-json-inside-bundle>

# 2. 用固定上游 catalog 本地 build bundle（不动 GitHub）
python3 scripts/build.py \
  --version "$VERSION" \
  --catalog-bundle-url "$CATALOG_BUNDLE_URL" \
  --expected-bundle-sha "$BUNDLE_SHA" \
  --expected-index-sha "$INDEX_SHA" \
  --output build

# 3. 看 build summary
jq '.included_count, .failed | length, .invalid | length, .skipped_unverified' \
  build/costrict-marketplace-bundle-v$VERSION/build-summary.json
# 看一下 failed/invalid 是否有系统性原因。按 per-plugin reason 决定
# 是改 PRUNE_PATTERNS / fetch 启发式，还是直接接受这次的损失。

# 4.（可选）抽样验证几个 plugin
for id in $(jq -r '.plugins[0:5][].id' build/costrict-marketplace-bundle-v$VERSION/manifest.json); do
  rm -rf /tmp/verify-$id
  git clone build/costrict-marketplace-bundle-v$VERSION/repos/plugins/$id.git /tmp/verify-$id
  ls /tmp/verify-$id/.claude-plugin/plugin.json
done

# 5. 推 bare repo 到 GitHub org（幂等、有限流、自动修空 repo）
./scripts/publish.sh build/costrict-marketplace-bundle-v$VERSION --skip-existing

# 6. 切 GitHub Release
scripts/release.sh "$VERSION"

# 7. 验证外部
gh release view "v$VERSION" --repo costrict-plugins-repo/costrict-plugin-marketplace
```

## 改 import.sh 后的 smoke test

当你改了 `import.sh` 本身时，先做本地端到端：

```bash
# 预创建空 bare repo
mkdir -p /tmp/git-srv && for r in $(cat build/costrict-marketplace-bundle-v$VERSION/repo-list.txt); do
  git init --bare --quiet /tmp/git-srv/$r.git
  git -C /tmp/git-srv/$r.git config http.receivepack true
done

# 起 bundle 里附带的 smart-HTTP server
python3 scripts/git-smart-http.py --root /tmp/git-srv --port 8848 &

# 跑解压后的 import.sh
cd build/costrict-marketplace-bundle-v$VERSION && ./import.sh http://127.0.0.1:8848

# 确认 marketplace 可被 clone
git clone http://127.0.0.1:8848/marketplace.git /tmp/verify-mp
cat /tmp/verify-mp/.claude-plugin/marketplace.json | jq '.plugins | length'

# 收尾
kill %1 && rm -rf /tmp/git-srv /tmp/verify-mp
```

## 版本号策略

遵循 `marketplace-publish` spec 里的 SemVer：

| 改动 | bump |
| --- | --- |
| 只是 `import.sh` / 文档 / build pipeline 修复，plugin 集合没变 | PATCH（`v0.1.0` → `v0.1.1`） |
| 加 / 删 plugin，没有破坏性的格式变化 | MINOR（`v0.1.0` → `v0.2.0`） |
| Bundle 布局、manifest schema、marketplace.json schema 变化 | MAJOR（`v0.x` → `v1.0.0`） |

多个客户成功落地后，可以直接切 `v1.0.0` 表示稳定。

## Release 一旦发布不可重写

我们不覆盖也不删除已发布的 release。如果某个 release 有问题，在 GitHub UI 里标成 "pre-release"，然后切新版本（PATCH 或 MINOR 按严重度）。客户可能已经下载了那个坏 bundle，删除会破坏审计链。

## 推 GitHub 的注意事项

用 `scripts/publish.sh`（不是 `build.py --publish` —— 那个虽然还在但维护得不勤；`publish.sh` 才有限流 + 空 repo 修复的逻辑）：

```bash
./scripts/publish.sh build/costrict-marketplace-bundle-v$VERSION --skip-existing
```

关键行为：

- **两阶段**：Phase 1 串行 create（2s 间隔 + rate-limit 退避），Phase 2 并行 `git push --mirror`（4 并发）
- **幂等**：用 `--skip-existing` 跳过 GH 上已有内容的 repo。已建但空的 repo 自动检测并在同一次运行里推内容
- **大概在 burst 138 个 create 后命中 GitHub secondary rate limit**。脚本会 sleep `120s × N`（最多 8 次重试）。如果耗尽就晚点再跑 —— `--skip-existing` 保留进度
- **不要同时跑两个 `publish.sh` 实例** —— 都会撞 rate limit 而且延长封禁
- **CI 使用 `--yes`** —— 本地人工执行默认保留确认提示，除非明确要自动化。

## 故障排查

完整目录见 [troubleshooting.zh-CN.md](./troubleshooting.zh-CN.md)。常见几个：

**publish 时撞 GitHub secondary rate limit** —— 静等 10-15 min 后用 `--skip-existing` 重跑

**org 里有些 plugin repo 是空的（无 refs）** —— 我们曾有过这个 bug 但已修：kill 当前 `publish.sh`，用 `--skip-existing` 重跑 —— 脚本启动时会探测空 repo 并把它们纳入 push 集合

**某个 plugin 一直 fetch 失败** —— 它的 `source_url` 可能 404 或搬家。看 `build-summary.json::failed`。要么去上游 `costrict-skills-repo` 修 catalog 条目，要么接受暂时的缺口

**可重现性破了** —— 同日两次 build 应产生相同 md5。如果不同，说明 mtime 漏进了 tar。检查 `scripts/build.py` 的 `make_tarball` —— 所有 `info.mtime` 和 `gzip.GzipFile(mtime=…)` 必须钉到 `ctx.build_date_iso`

**build workers=1（慢）** —— 旧版 `build.py` 没 `ThreadPoolExecutor`。拉最新

## 项目边界

本项目跟 `costrict-web` 解耦。不动：

- Go server 或其 endpoint
- `capability_items` 表或任何 DB
- `/hub` 收藏 API

这个代码边界不代表公共发布可以使用不同输入。公共发布必须使用和 web ingest 相同的固定 catalog bundle 以及内部 index SHA。
