# 私有化部署 (Private Deployment / 内网)

[English](./private-deployment.md) · **简体中文**

本文档是给 IT 管理员看的 —— 在不能访问公网（或受限网络）的环境下，把 costrict-plugins marketplace 导入到客户内网。导入完成后，开发者只需 `csc plugin marketplace add …` 指向客户内网 git 服务器，就能用完整的 plugin 目录，全程**无需** GitHub 访问。

如果你的工作机能直接访问 github.com，不需要走这条路 —— 见 [public-usage.zh-CN.md](./public-usage.zh-CN.md) 的一行命令方案。

---

## 架构总览

```
+--------------------+   1. 下载       +-------------------+
| github.com         | --------------> | 公网跳板机        |
| (我们的 release)   |                 | (下载 .tar.gz)    |
+--------------------+                 +---------+---------+
                                                 | 2. 传输（U 盘 / DMZ）
                                                 v
                                  +--------------+---------+
                                  | 解压 bundle            |
                                  | 跑 ./import.sh <URL>   |
                                  +-----------+------------+
                                              | 3. git push --mirror x 677
                                              v
                                  +--------------+---------+
                                  | 内网 git 服务器        |
                                  | (Gitea / GitLab / etc) |
                                  +-----------+------------+
                                              | 4. csc clone
                                              v
                                  +--------------+---------+
                                  | 开发者工作机           |
                                  | csc plugin install ... |
                                  +------------------------+
```

bundle 是单个 **`.tar.gz`**（v0.1.0 约 75 MB），里面是每个 verified plugin 的 bare git repo + 一份 `marketplace.json` 模板 + `import.sh` 加载脚本。`import.sh` 把内容推到客户内网 git 服务器后，开发者把 `csc` 指向 `<内网 base URL>/marketplace.git` 就接入了。

---

## 前置要求

### 跑 `import.sh` 的机器

| 要求 | 说明 |
| --- | --- |
| `bash` 4.0+ | macOS / Linux / WSL 都行 |
| `git` 2.30+ | 更新的也行 |
| `find` / `xargs` / `sed` | GNU 或 BSD 任一 |
| 网络可达内网 git 服务器 | HTTPS / SSH 都行 |
| `git config credential.helper` 已配 **或** 服务器允许匿名 push | 脚本本身不会弹 prompt |

### 内网 git 服务器

| 要求 | 说明 |
| --- | --- |
| 支持 smart-HTTP（或 SSH）协议 | 即真实 git server，不是纯静态文件 mirror。已测：Gitea ≥ 1.9 / GitLab CE/EE / Forgejo / GitHub Enterprise / Bitbucket DC |
| 支持 auto-create-on-push **或** admin 愿意按 `repo-list.txt` 预创建 | 主流 server 都能开 auto-create |
| 每次导入预留 ~150-300 MB 磁盘（首次后续只传 delta） | |
| `git push --mirror` 不被 pre-receive hook 拒（如有签名提交策略，需要为 `costrict-build` 作者开白） | |

---

## 一步步走

### 1. 在公网机器上下载 bundle

```bash
gh release download v<version> \
    --repo costrict-plugins-repo/costrict-plugin-marketplace \
    --pattern 'costrict-marketplace-bundle-v<version>.tar.gz*'
```

你会得到两个文件：`costrict-marketplace-bundle-v<v>.tar.gz` 和 `.tar.gz.sha256`。

不能用 `gh` 的话，直接打开 release 页面下载：
<https://github.com/costrict-plugins-repo/costrict-plugin-marketplace/releases>

### 2. 校验

```bash
shasum -a 256 -c costrict-marketplace-bundle-v<v>.tar.gz.sha256
```

两个文件都必须 verify OK 才进行下一步。

### 3. 传入内网

任何方式都行：U 盘 / 内部文件 mirror / DMZ scp / 签名包管理系统等等。bundle 是单文件就是为了能干净走过这些受限通道。

### 4. 解压

```bash
tar -xzf costrict-marketplace-bundle-v<v>.tar.gz
cd costrict-marketplace-bundle-v<v>
ls
# manifest.json  marketplace.json.tmpl  import.sh  README.md  repo-list.txt  repos/  build-summary.json
```

### 5.（可选）在 git 服务器上预创建 repo

**如果你的 server 支持 auto-create-on-push，跳过本步。**

如果 server 要求 push 前 repo 先存在，bundle 里有 `repo-list.txt` —— 一行一个 repo 名，约 678 条 —— 你（或 git admin）可以批量创建。

Gitea：
```bash
TOKEN=ghp_xxx  # 有 repo create 权限的 personal access token
ORG=costrict   # 你想让 plugin 落到哪个 namespace
while read repo; do
  curl -s -u "admin:$TOKEN" \
       -H 'Content-Type: application/json' \
       -X POST "https://git.internal.corp/api/v1/orgs/$ORG/repos" \
       -d "{\"name\":\"$repo\",\"private\":false}" >/dev/null
done < repo-list.txt
```

GitLab（需要 group 的数字 ID）：
```bash
GROUP_ID=42
TOKEN=glpat_xxx
while read repo; do
  curl -s --header "PRIVATE-TOKEN: $TOKEN" \
       "https://gitlab.internal.corp/api/v4/projects?name=$repo&namespace_id=$GROUP_ID&visibility=public" \
       -X POST >/dev/null
done < repo-list.txt
```

Forgejo：API 跟 Gitea 一样。

### 6. 跑导入

```bash
./import.sh https://git.internal.corp/costrict
```

唯一参数是 git 服务器的 **base URL** —— 每个 plugin 被推到 `<base>/<plugin-id>.git`，marketplace 索引推到 `<base>/marketplace.git`。末尾斜杠会被自动去掉。

跑下来通常 **5-15 分钟**（LAN 内 8 并发 push）。**幂等** —— 用相同参数重跑安全且快（git delta 传输让未变 plugin 几乎瞬间过）。

样例输出：

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

### 7. 在开发者工作机上接入 `csc`

```bash
csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git
csc plugin list                                # 约 677 个 plugin
csc plugin install <plugin-name>@costrict-plugins
```

完事。开发者就能装 marketplace 里任何 plugin；`csc` 按需从内网 git 服务器拉取对应 plugin 的 bare repo。

---

## 升级到新 bundle

1. 在公网机器上下载新 bundle（`v<new>`）
2. 校验、传输、解压 —— 同步骤 1-4
3. 重跑 `./import.sh https://git.internal.corp/costrict` —— git 的 delta 机制保证未变 plugin 是 no-op；只有改动的 plugin 才会传字节
4. 在开发者机器上 `csc plugin marketplace update costrict-plugins` 刷新客户端索引

**不需要**清理上次的导入。

---

## 故障排查

完整目录见 [troubleshooting.zh-CN.md](./troubleshooting.zh-CN.md)。最常见几个：

| 现象 | 快速修法 |
| --- | --- |
| `fatal: repository '…' not found` 在 push 时 | 服务器没 auto-create。按 `repo-list.txt` 预创建（步骤 5） |
| `error: failed to push some refs … (pre-receive hook declined)` | 服务器策略拒绝首次提交。把 `costrict-build` 作者加白名单 |
| 部分 plugin push 超时 | LAN 拥塞。重跑 `./import.sh` —— `push --mirror` 幂等 |
| 导入后 `csc plugin install` 失败 | 先验证 plugin repo：`git clone https://git.internal.corp/costrict/<plugin-id>.git`，应该能 clone 且有 `.claude-plugin/plugin.json` |
| 有些 repo 在服务器上存在但是空（0 refs） | 再跑一次 `./import.sh`；`git push --mirror` 幂等，会把内容补上 |

---

## 导入了什么

每个 plugin 的内容在打包前已**裁剪**到运行时必需文件：

**保留：**
- `.claude-plugin/`（所有内容，含 `plugin.json`）
- `skills/` / `commands/` / `agents/` / `hooks/`（递归）
- 顶层 `LICENSE*`、`README.md` / `README.txt`

**裁剪：**
- 源码目录：`src/` / `lib/` / `test/` / `tests/` / `spec/` / `examples/` / `demo/` / `docs/`
- 构建产物：`node_modules/` / `dist/` / `build/` / `target/` / `__pycache__/`
- > 500 KB 的媒体：`*.png` / `*.jpg` / `*.gif` / `*.mp4` / `*.mov`
- 构建配置：`package.json` / `tsconfig.json` / `Cargo.toml` / `pyproject.toml` 等

每个 plugin 的原 LICENSE 都保留。catalog 来源 SHA 写在 `manifest.json::catalog_sha`，便于审计。

---

## 客户**不要**做的事

- **不要直接编辑内网 git 服务器上的 plugin repo。** 它们每次发 release 都从上游重新生成；你的改动会在下次 `import.sh` 时被覆盖。
- **不要在升级之间删 `marketplace.git`。** 它不是"marketplace 本身" —— 它只是 `csc` 读的索引文件。误删了直接重跑 `./import.sh` 就会重建。
- **不要把 `repo-list.txt` 当作永久参考。** 它是 per-release 产物，未来 release 可能加 / 删 plugin。

维护者侧的话题（发 release、版本号策略等）见 [for-maintainers.md](./for-maintainers.md)。
