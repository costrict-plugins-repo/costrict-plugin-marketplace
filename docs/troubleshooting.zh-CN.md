# 故障排查 (Troubleshooting)

[English](./troubleshooting.md) · **简体中文**

build / publish / 客户 import 过程中真实遇到过的故障模式 —— 含修法。

## 客户侧：跑 `import.sh`

### push 时报 `fatal: repository '<url>' not found`

你的 git 服务器不支持 auto-create-on-push。

**修法：** 按 `repo-list.txt`（一行一个，约 678 个）批量预创建。各 server 的 API snippet 见 [private-deployment.zh-CN.md 第 5 步](./private-deployment.zh-CN.md#5可选在-git-服务器上预创建-repo)。

### 报 `error: failed to push some refs … (pre-receive hook declined)`

服务器有 push 策略（要求签名 commit / GPG / 空 repo 分支保护等）拒绝了首次推送。

**修法：** 把 `costrict-build <build@costrict.local>` 作者或 `main` 分支加白名单到 costrict namespace 下；或在导入期间临时关掉相关规则。

### 很多 plugin 报 `Connection reset by peer` / `HTTP 500`

git server（特别是用裸 `git-http-backend` CGI 的较小服务）可能在 8 并发 push + chunked-transfer 大 body 下卡住。症状：大 plugin 失败，小 plugin 都过。

**修法：** 重跑 `./import.sh` —— `push --mirror` 幂等，失败的会重试。或改 `import.sh` 里 `-P 8` 为 `-P 4` 降并发。

### 报 `Everything up-to-date` 但 plugin 显示失败

某些 git server 在 push 中途返回 HTTP 500，git 客户端重试时显示 `Everything up-to-date`（因为部分 ref 已落，但其他没）。脚本把非零退出当失败 —— 实际上仓库可能是半推。

**修法：** 重跑 `./import.sh`。第二次会检测到 ref 不匹配并强制推完剩余内容（`--mirror` 语义）。

### LAN 很快但导入慢

默认 8 并发。瓶颈通常在 git server 的 `receive-pack` 吞吐。

**修法：** 把 `import.sh` 里 `-P 8` 改大（比如 `-P 16`）。注意观察服务器的 CPU / IO 不要打爆。

### 代理捣乱

如果机器有 `HTTPS_PROXY` / `HTTP_PROXY`，git 会继承。对纯内网目标常常出错。

**修法：** 把内网 git 主机加到 `no_proxy`：
```bash
export no_proxy=git.internal.corp,$no_proxy
./import.sh https://git.internal.corp/costrict
```

### 导入成功但 `csc plugin install` 失败

大概率是 `csc` 配置问题，不是 import 问题。先验证服务器上的 plugin repo：
```bash
git clone https://git.internal.corp/costrict/<plugin-id>.git /tmp/verify
ls /tmp/verify/.claude-plugin/plugin.json  # 必须存在
```

clone 成功且有 `plugin.json` 的话，`csc` 应该能装。常见 csc 端问题：
- `csc plugin marketplace list` 看不到 `costrict-plugins` —— 重新 add marketplace
- import 后跑 `csc plugin marketplace update costrict-plugins` 刷新
- 检查 `~/.claude/settings.json` 里有 marketplace 条目

### 服务器上有些 plugin repo 是空的（0 refs）

干净的 `./import.sh` 一次跑通的话不应该出现这种情况，但脚本中途被打断时可能发生。

**修法：** 重跑 `./import.sh` —— 脚本每次都对 bundle 里每个 plugin 推一次；空 repo 在下次重跑时会被填上。

---

## 维护者侧：build bundle

### `WARNING fetch failed: <plugin> (clone timed out)`

某个 plugin 上游 repo 真的慢（大 LFS / 地区慢 / 网络抖动）。

**修法：** 二选一：
- 提高 `scripts/build.py` 里的 `CLONE_TIMEOUT_SECONDS`（默认 90s）然后重跑 —— build 是可续的，已建 bare repo 会自动 skip
- 接受损失，写进 release notes；通常下次上游 sync 会自然修好

### `WARNING plugin.json not found: <plugin>`

上游 repo 没有 `.claude-plugin/plugin.json` —— 要么 plugin 作者没按 marketplace plugin 打包，要么 catalog 的 `marketplace_repo` / `plugin_name` 字段不对。

**修法：** 手工看一下上游。如果是真错分类了，去 `costrict-skills-repo` 报 issue。

### 可重现性破了：同日两次 build 产生不同 `md5`

大概率某处漏了非零 mtime。检查：
- `manifest.json::built_at` 是不是 `ctx.build_date_iso`（UTC 0 点）
- `build-summary.json::built_at` 同样
- bare repo commit 是不是有 `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` 钉死
- `make_tarball` 是不是用 `gzip.GzipFile(mtime=epoch, …)` 并对每个 tar entry 设 `info.mtime = epoch`

定位办法：diff 两个解压后的 bundle 目录，通常 1-2 个文件就暴露问题。

### build 是串行的、很慢

默认 `--workers 8` 应该并行 fetch。确认：
```bash
python3 scripts/build.py --version <v> --workers 8 -v
```

如果 workers=1，说明用的是没打 `ThreadPoolExecutor` patch 的旧 `build.py`。

---

## 维护者侧：推到 GitHub (`publish.sh`)

### `GraphQL: You have created too many repositories, too quickly`

GitHub secondary rate limit（创建内容的）。burst 创建（短时间内 ~138+ 个 repo）会触发。

**修法：** 脚本会自动退避（`120s × N`，最多 8 次）。如果耗尽：**完全停掉所有 `gh` 流量 10-15 分钟**，然后用 `--skip-existing` 重跑。**不要并行跑多个 `publish.sh`** —— 那只会延长封禁。

### `Get "https://api.github.com/...": net/http: TLS handshake timeout`

短暂网络抖动。脚本会把它计为 CREATE_FAIL 但不会在本次运行内自动重试。

**修法：** 用 `--skip-existing` 重跑 `publish.sh` —— 打过 patch 的脚本会检测出"已建但空"的 repo 并补推内容（同时创建仍然缺的）。

### publish 完成后部分 repo 仍是空的

这是个真实 bug，已修（在 v0.1.0 的 `publish.sh` 里）。症状：`gh api orgs/<org>/repos` 显示 200+ repo，但 `git ls-remote` 返回 0 refs 的不少。

**原因：** `publish.sh` 的 Phase 1 串行 create repo，Phase 2 并行推内容。如果脚本在两阶段之间被杀，已建未推的就空了。老版的 `--skip-existing` 会把空 repo 当作"已存在跳过"。

**修法：** 现在的 `publish.sh` 启动时会做并行 `git ls-remote` 探测，把空 repo 纳入 push 集合，不管有没有 `--skip-existing`。直接重跑脚本即可。

### 想全清重来

不要一个个删 repo。用批量脚本：
```bash
gh api orgs/costrict-plugins-repo/repos --paginate --jq '.[].name' | while read repo; do
  gh repo delete "costrict-plugins-repo/$repo" --yes
done
```

**注意：** GitHub 也可能对 DELETE 限流。

---

## 测试用的 smart-HTTP server (`scripts/git-smart-http.py`)

只在用我们的本地 server 模拟客户部署时才会遇到。真实的客户 git server（Gitea / GitLab 等）都正确处理这些场景。

### 大 plugin push 报 `HTTP 500` / `Connection reset`

最小化的 smart-HTTP server 之前不支持 HTTP `Transfer-Encoding: chunked`，而 `git push` 在 pack 大于 ~1 MB 时会用 chunked。**已在 v0.1.0 修了** —— handler 现在解码 chunked body 再转给 `git-http-backend`，并根据解码后大小设置 `CONTENT_LENGTH`。

老版本碰到这个 bug 的话，更新 `scripts/git-smart-http.py` 到最新即可。

### `ERROR: could not locate git-http-backend on this system`

CGI binary 不在常见路径上。脚本会试几个标准位置和 `git --exec-path`。都失败的话：

```bash
find / -name 'git-http-backend' 2>/dev/null   # 手动找
# 找到后改 scripts/git-smart-http.py，把路径加进 GIT_HTTP_BACKEND_CANDIDATES
```

### 并发 push 偶尔丢

脚本用 `ThreadingHTTPServer` + 每个请求一个 `git-http-backend` 子进程。8+ 并发推大 repo 时，OS 可能 queue 一些连接，客户端看到偶发掉线。

**修法：** 重跑客户侧的 `./import.sh`（幂等），直到成功率 100%。

---

## Catalog 问题

这些是上游问题 —— 去 <https://github.com/zgsm-ai/everything-ai-coding/issues> 报，不要在这个 repo 报。

### 某个 plugin 的源 repo 404 或搬家

`costrict-skills-repo` 下次上游 sync 应该会捕捉。在那之前那条 entry 每次 build 都会 fail 报 `fetch: git error 128`。

### 某个 plugin 的 `marketplace_verified` 是 `true` 但内容坏了

`verified` 是启发式打的。如果 plugin build 成功但 `csc plugin install` 用不了 / plugin 不工作，那条 catalog entry 需要复审。

---

## 在哪里报哪类问题

| 问题归属 | 报到 |
| --- | --- |
| `import.sh` / bundle 格式 / `publish.sh` | `costrict-plugins-repo/costrict-plugin-marketplace` |
| `csc` CLI 行为 | csc 仓库（独立项目） |
| 某个 plugin 的内容 / 可用性 | `costrict-skills-repo`（上游 catalog） |
| plugin 作者自己的 repo | 上游 `<owner>/<plugin>` 直接报 |
