# 公网用户使用指南 (Public Usage)

[English](./public-usage.md) · **简体中文**

如果你的 `csc` 工作机能访问 `github.com`，你**不需要** bundle、`import.sh` 或任何私有化部署工作。我们 GitHub 上托管的 marketplace 就跟 Anthropic 的 `claude-plugins-official` 一样 —— 一行命令搞定，不用下载。

## 接入

```bash
csc plugin marketplace add https://github.com/costrict-plugins-repo/marketplace.git
```

`csc` 会克隆 marketplace 索引（几 KB），并以名字 `costrict-plugins` 注册。

## 浏览 & 安装

```bash
# 列出所有可用 plugin
csc plugin list

# 按关键词搜索
csc plugin list | grep -i security

# 装一个 plugin（csc 此时才从对应仓库拉取那一个 plugin 的内容）
csc plugin install <plugin-name>@costrict-plugins
```

`csc` 只在你装某个 plugin 时才从 `https://github.com/costrict-plugins-repo/<plugin-id>.git` 拉取它的 bare repo —— marketplace 索引本身始终很小（渐进披露 / lazy loading）。

## 更新

```bash
# 刷新 marketplace 索引（捕捉新增 / 移除）
csc plugin marketplace update costrict-plugins

# 重装某个 plugin 取新版本
csc plugin install <plugin-name>@costrict-plugins --force
```

## 卸载

```bash
csc plugin uninstall <plugin-name>@costrict-plugins
csc plugin marketplace remove costrict-plugins
```

## 在哪里看

| 项目 | 地址 |
| --- | --- |
| Marketplace 列表 | `https://github.com/costrict-plugins-repo/marketplace` |
| 单个 plugin repo | `https://github.com/costrict-plugins-repo/<plugin-id>` |
| 所有 plugin（org 主页） | `https://github.com/orgs/costrict-plugins-repo/repositories` |

每个 plugin 自己的 LICENSE 和 README 都在它自己的 repo 里 —— `csc` 安装时会一并拉取。我们不修改任何 plugin 的源码；我们只镜像 [`costrict-skills-repo`](https://github.com/zgsm-ai/everything-ai-coding) 里 verified=true 的子集，并裁剪到运行时必需文件（`.claude-plugin/`、`skills/`、`commands/`、`agents/`、`hooks/`、`LICENSE`、`README.md`）。

## FAQ

**Q: 跟 Anthropic 的 `claude-plugins-official` 有什么区别？**

我们镜像的范围更大 —— ~770 个 verified plugin，来自 3 个上游源（`claude-plugins-official`、`superpowers-marketplace`、`claude-plugins-dev`）。Anthropic 官方的 marketplace 只有 ~178 个他们亲自审过的 plugin。

**Q: 我可以投稿 plugin 到这个 marketplace 吗？**

我们只是 [`costrict-skills-repo`](https://github.com/zgsm-ai/everything-ai-coding) catalog 的镜像 —— 去那边开 issue 或 PR。下次发 release 时我们的 build pipeline 会自动同步进来。

**Q: 数据新鲜度怎么样？**

每个 `costrict-marketplace-bundle-v<ver>` release 是一个快照。release notes 里写了构建时 catalog 的 SHA。要持续更新就跑 `csc plugin marketplace update`（不依赖我们的 release 周期）。

**Q: 装某个 plugin 失败报 404 之类的怎么办？**

那个 plugin 的上游 repo 可能搬家或转私有了。来 [marketplace repo](https://github.com/costrict-plugins-repo/costrict-plugin-marketplace/issues) 提 bug —— 我们要么修 catalog，要么下次 build 时移除它。

**Q: 我公司屏蔽了 `github.com`，怎么办？**

走私有化部署 —— 看 [docs/private-deployment.zh-CN.md](./private-deployment.zh-CN.md)。
