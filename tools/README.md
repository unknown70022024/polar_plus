# tools/

仓库维护脚本，不参与管线运行。

## `credentials.py` — 凭据从哪来

所有需要访问 GitHub 的脚本都从这里取 token，**不要各自 `read_text()`**。
本项目换过一次凭据位置，分散读取的话漏掉一处只会在运行时变成 401 ——
看着像权限问题，其实是路径问题。

解析顺序（先命中先用）：

| 顺序 | 位置 | 说明 |
|---|---|---|
| 1 | `GH_TOKEN` / `GITHUB_TOKEN` 环境变量 | CI 与非交互场景，凭据不落盘 |
| 2 | `~/keys/token` | **当前使用**。classic PAT，scope `repo` + `workflow` + `write:packages` |
| 3 | `~/keys/github_pat` | 回退。细粒度 PAT |
| 4 | `~/.gh-token` | 早期位置 |

### 为什么最终用 classic PAT

细粒度 PAT 的**授权仓库列表在创建时固定**。新建一个仓库后，该仓库可能仍不在
token 可见范围内，而 GitHub 对未授权仓库**一律返回 404**（与"仓库不存在"无法
区分，这是刻意设计，防止用 token 枚举私有仓库）。排查时表现为：

```
polar_plus  → HTTP 200   token 有权限
gcc-probe   → HTTP 404   token 无权限，但仓库确实存在
```

classic PAT 带 `repo` scope 时自动覆盖名下所有仓库，包括以后新建的，不存在这个
问题——代价是权限粒度粗。个人项目用 classic 更省事。

自检（只报告来源与权限，不打印 token）：

```bash
python tools/credentials.py
```

## `api_push.py` — 推送

**本机 `github.com:443` 完全不通**（curl 返回 HTTP 000），只有 `api.github.com`
可达，所以 `git push` 用不了。这个脚本走 GitHub Git Data API 逐个上传
blob / tree / commit。

```bash
python tools/api_push.py unknown70022024/polar_plus
```

### 空仓库要先引导

GitHub 不允许在**一个提交都没有**的仓库上创建 blob / tree / commit 对象，
`/git/blobs`、`/git/trees`、`/git/commits` 全部返回 `409 Git Repository is
empty`。脚本会先探测分支 ref，若为 404/409 就用 Contents API 写一个
`.bootstrap` 占位文件，把根提交和默认分支建出来，之后正常推送。占位文件会被
第一棵真实树覆盖（那棵树里没有它），所以只留在历史里，不影响最终内容。

这个坑在 polar_plus 上踩过一次（当时是手工先建初始提交才绕过去）。

### 为什么需要 `.git/polar-remote-map.json`

GitHub 会重新计算提交 sha，所以远端提交与本地提交**内容相同但 sha 不同**。
这带来两个问题：

1. `git rev-list <远端头>..HEAD` 用不了 —— 远端头在本地不存在
2. 每次推送都得重建整段历史，越推越慢

脚本在 `.git/polar-remote-map.json` 里记录「本地 sha → 远端 sha」的映射，
下次推送时用远端头反查出对应的本地提交，只重放增量。映射表丢了会自动
退化成全量重放，功能不受影响，只是慢。

### 注意

- 推送只改远端分支指针，**不会**改动本地 `origin/main` 追踪引用；本地和远端
  的 sha 本来就是两套。判断同步状态要看内容，不要看 `git status` 的
  "ahead/behind"。
