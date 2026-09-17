# tools/

仓库维护脚本，不参与管线运行。

## `api_push.py` — 推送

**本机 `github.com:443` 完全不通**（curl 返回 HTTP 000），只有 `api.github.com`
可达，所以 `git push` 用不了。这个脚本走 GitHub Git Data API 逐个上传
blob / tree / commit。

```bash
python tools/api_push.py unknown70022024/polar_plus
```

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
- 需要 `~/.gh-token`（600 权限，仓库外），scope 要含 `repo` + `workflow`
  （`workflow` 是推 `.github/workflows/` 必需的）。
