# costrict-marketplace-bundle

[English](./README.md) · **简体中文**

本 bundle 把完整的 `costrict-plugins` plugin 目录（镜像自上游公开源）以 bare git repo 形式打包，附带 `import.sh` 把它们一键导入到内网 git 服务器。导入后，把 `csc` 指向内网 marketplace URL，全部 plugin 就在防火墙内可用。

更完整的内容 —— 架构、prereq、服务器兼容矩阵、故障排查 —— 见 [costrict-plugin-marketplace 项目文档](https://github.com/costrict-plugins-repo/costrict-plugin-marketplace/tree/main/docs)。

## Bundle 内容

| 路径 | 用途 |
| --- | --- |
| `manifest.json` | bundle 版本、构建时间、catalog 来源、每个 plugin 的清单（id / name / version / size） |
| `marketplace.json.tmpl` | `csc` 可读的 marketplace 索引；`{{BASE_URL}}` 占位符由 `import.sh` 替换 |
| `repos/plugins/<id>.git/` | 每个 plugin 一个 bare git repo，可直接 `git push --mirror` |
| `repo-list.txt` | 本 bundle 期望的所有 repo 名清单（`marketplace` + plugin id）。如果你 git 服务器不支持 auto-create，交给 git admin 用这个清单批量建 |
| `import.sh` | 一键导入脚本，对着内网 git URL 跑一次 |
| `build-summary.json` | 构建统计（成功 / 失败 / invalid / skipped 数量） |

## 前置要求

跑 `import.sh` 的机器：

- `bash` 4.0+
- `git` 2.30+
- `find` / `xargs` / `sed`（GNU 或 BSD 都行）
- 网络可达内网 git 服务器
- 服务器允许匿名 push **或** 你已经配好 `git config credential.helper` 让 push 不弹 prompt

内网 git 服务器满足其中一个：

- 开启 auto-create-on-push（Gitea / GitLab / Forgejo 都支持）
- **或** admin 按 `repo-list.txt` 提前批量建 repo

## 用法

```bash
# 1) 解压
tar -xzf costrict-marketplace-bundle-v<version>.tar.gz
cd costrict-marketplace-bundle-v<version>/

# 2) 导入（LAN 内 8 并发，约 5-15 分钟）
./import.sh https://git.internal.corp/costrict

# 3) 让 csc 接入新 marketplace
csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git

# 4) 验证
csc plugin list
csc plugin install <plugin-name>@costrict-plugins
```

`import.sh` 唯一参数是 git 服务器的 **base URL**：每个 plugin 被推到 `<base>/<plugin-id>.git`，marketplace 索引推到 `<base>/marketplace.git`。末尾 `/` 自动去掉。

## 预创建 repo（需要时）

如果服务器不支持 auto-create-on-push，bundle 里的 `repo-list.txt` 列出了所有 repo 名。交给 admin：

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

## 故障速查

**`fatal: repository '…' not found`** —— git 服务器没 auto-create。按上面 snippet 用 `repo-list.txt` 预建。

**`error: failed to push some refs … (pre-receive hook declined)`** —— 服务器策略拦截。要么放开 `costrict-build <build@costrict.local>` 作者的策略，要么先预建 repo 让首次 push 走非 policy 路径。

**少数 push 失败** —— 并发 push 经 HTTP 在较小 git server 上会有压力。`import.sh` 不会中断，会在最后汇报。要重试失败的就**用同样参数重跑** —— `git push --mirror` 幂等。

**慢** —— `import.sh` 默认 `-P 8` 并发。LAN 强可以改大；服务器吃不消改小。

**有些 repo 在服务器上但显示 "empty repository"** —— 重跑 `./import.sh`；幂等推会把它们填上。

**公司代理捣乱** —— 把内网 git 主机加 `no_proxy`：
```bash
export no_proxy=git.internal.corp,$no_proxy
./import.sh https://git.internal.corp/costrict
```

完整目录见顶部那个项目文档链接。

## 验证

`./import.sh` 跑完后：

1. `csc plugin marketplace add <base-url>/marketplace.git` —— 应报告新 marketplace 已添加
2. `csc plugin marketplace list` —— 能看到 `costrict-plugins`
3. `csc plugin list` —— 数量大致跟 `manifest.json::plugin_count` 一致
4. 装一个：`csc plugin install <pick-one>@costrict-plugins`

四步都过就闭环了。

## 升级

新 bundle 出了就重跑同样流程。`git push --mirror` 幂等 —— 没变的 plugin 不会重传字节，只有变了的才传。**不需要**清理上次的导入。

## License & 来源

本 bundle 是公开第三方 plugin 的镜像。每个 plugin 自己的 LICENSE 都保留在它的 bare repo 内。`manifest.json::catalog_source` 是来源 catalog，`catalog_sha` 是构建时的上游快照 SHA。
