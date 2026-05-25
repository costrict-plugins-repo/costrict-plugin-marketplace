# costrict-plugin-marketplace

[English](./README.md) · **简体中文**

把上游 `costrict-skills-repo` 的 plugin catalog 镜像为两种可用形态：

1. **公网 / Public** —— 直接让 `csc` 接我们 GitHub 上托管的 marketplace；一行命令，不用下载任何东西。
2. **私有化 / Private** —— 下载一个 `.tar.gz` bundle，在客户内网里跑 `import.sh` 把所有 plugin 推到客户自己的 git server。之后整个 plugin 目录就在防火墙内可用，全程不需要访问 GitHub。

两种模式渲染出来的 `marketplace.json` schema 是一样的，per-plugin bare git repo 也一样，所以 `csc` 用户的体验和操作完全一致，不管走哪条路。

## 快速开始

### 公网用户（能访问 GitHub）

```bash
csc plugin marketplace add https://github.com/costrict-plugins-repo/marketplace.git
csc plugin list                                  # 约 770 个 verified plugin
csc plugin install <plugin-name>@costrict-plugins
```

就这样 —— 验证步骤和 FAQ 详见 [docs/public-usage.zh-CN.md](docs/public-usage.zh-CN.md).

### 私有化客户（内网 / 隔离环境）

```bash
# 1. 在能访问互联网的机器上下载 bundle 和 checksum
gh release download v0.1.0 \
    --repo costrict-plugins-repo/costrict-plugin-marketplace \
    --pattern 'costrict-marketplace-bundle-v0.1.0.tar.gz*'

# 2. 校验
shasum -a 256 -c costrict-marketplace-bundle-v0.1.0.tar.gz.sha256

# 3. 把 tarball 传到内网（U 盘 / DMZ / 内部 mirror 都行）
#    在能访问内网 git 服务器的机器上解压：
tar -xzf costrict-marketplace-bundle-v0.1.0.tar.gz
cd costrict-marketplace-bundle-v0.1.0

# 4. 把所有 plugin + marketplace 推到内网 git 服务器
./import.sh https://git.internal.corp/costrict

# 5. 在开发者机器上接入 csc
csc plugin marketplace add https://git.internal.corp/costrict/marketplace.git
csc plugin list
```

完整 SOP、prereq、服务器兼容矩阵、troubleshooting 都在 [docs/private-deployment.zh-CN.md](docs/private-deployment.zh-CN.md)。

## Bundle 内容

每个 release 输出一个 `costrict-marketplace-bundle-v<semver>.tar.gz`，结构如下：

```
costrict-marketplace-bundle-v<ver>/
├── manifest.json              # bundle 元数据 + plugin 列表（按 id 排序，含大小）
├── marketplace.json.tmpl      # csc 可读 marketplace 索引；{{BASE_URL}} 由 import.sh 替换
├── repos/plugins/<id>.git/    # 每个 plugin 一个 bare git repo（v0.1.0 约 677 个）
├── repo-list.txt              # 所有 repo 名清单；交给 git admin 用于预创建
├── import.sh                  # 客户端 loader（bash 4+，无额外依赖）
├── README.md                  # bundle 自带的 quickstart + troubleshooting
└── build-summary.json         # 构建统计（成功 / 失败 / invalid 数量）
```

**可重现**：相同 catalog 同日两次 build 产生 byte-identical 的 tar.gz。

## 仓库结构

```
costrict-plugin-marketplace/
├── scripts/
│   ├── build.py               # fetch + prune + bare-repo + bundle pipeline
│   ├── publish.sh             # 推送 bundle 到 GitHub org（带 throttle、幂等）
│   ├── release.sh             # gh CLI release helper（打 tag + 上传）
│   └── git-smart-http.py      # 本地 smart-HTTP git server（用于测试）
├── bundle-assets/
│   ├── import.sh              # 一字不差地嵌入每个 bundle
│   └── BUNDLE_README.md       # 嵌入 bundle 作为客户看到的 README.md
├── docs/
│   ├── public-usage.md        # 公网 csc 用户
│   ├── private-deployment.md  # 客户 admin 落地 bundle 的 SOP
│   ├── for-maintainers.md     # 怎么 build + publish + 发 release
│   ├── troubleshooting.md     # 错误目录
│   └── *.zh-CN.md             # 中文版（本文件就是其中之一）
├── LICENSE                    # MIT
└── README.md / README.zh-CN.md
```

## 命名约定

| 项目 | 值 |
| --- | --- |
| GitHub org | `costrict-plugins-repo` |
| Plugin repos | `costrict-plugins-repo/<plugin-id>`（扁平，无 namespace 前缀） |
| Marketplace 索引 repo | `costrict-plugins-repo/marketplace` |
| 构建 pipeline repo | `costrict-plugins-repo/costrict-plugin-marketplace`（本仓库） |
| `marketplace.json::name` | `costrict-plugins` |
| Release tag | `v<semver>` |
| Bundle 文件名 | `costrict-marketplace-bundle-v<semver>.tar.gz` |

## 与 `costrict-web` 的关系

本项目**不**会动：

- `costrict-web` Go server
- `capability_items` 表或任何数据库
- `/hub` 收藏 API

代码仓库仍然分离，但公共发版按固定上游 `catalog-bundle.tar.gz` 及其内部 index SHA 协同。mirror 必须使用和 `costrict-web` ingest 相同的上游 catalog 构件构建，确保 web 可见的 plugin 都能从 `costrict-plugins` 安装。

## License

[MIT](./LICENSE)
