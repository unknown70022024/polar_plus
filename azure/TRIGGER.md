# 定时触发的拆分：Azure 管时钟，GitHub 管算力

> 状态：**全部完成并端到端验证通过**，包括状态页。唯一遗留是下文提到的本机网络访问限制。
> `azure/OPERATIONS.md` 描述的是拆分之前的架构，仍然准确，但不再是当前形态。

## 0. 验证记录

全链路实跑，不是推断：

| 环节 | 结果 |
|---|---|
| Azure 手动执行 `polar-trigger` | Succeeded，23 s（含冷启动拉 132 MB 镜像）|
| 容器内派发 | `POST ... dispatches` → **HTTP 204，1.450 s** |
| 对应 GitHub run | #557，`pipeline` + `deploy` **全部 success** |
| 线上 `root.json` | `timestamp: 20260918_060000`，`version: 84629b87a2f5` |
| 产物可达 | px/py/nz.jpg、storms.json（52 KB）、aurora.json 均 HTTP 200 |

`version` 是新 commit 的 sha 前 12 位——版本标记机制按设计工作。

**过程中由这次真实运行抓出一个我引入的 bug**：重写工作流步骤时漏了 `env: OUTPUT_DIR`，
输出落到 `polar_plus/output/` 而后续读 `output/latest/root.json`，于是 run.py 明明成功、
发布也写完了，却在最后一行 `FileNotFoundError` 退出 1（run #556）。整条工作流看似在跑，
实际全废。已修复并补了显式检查。**这个 bug 靠读代码没看出来。**

## 1. 为什么改

GitHub Actions 的 `schedule:` 对需要固定节奏的临近预报不可用。取 13 天、60 次定时运行的实测（2026-09-06 → 09-18）：

| 指标 | 实测 | 设计 |
|---|---|---|
| 每天触发次数 | 2-6（多数 4-5） | 12 |
| 相邻间隔 中位数 | 4.57 h | 2 h |
| 相邻间隔 均值 | 4.89 h | 2 h |
| 相邻间隔 最大 | **7.90 h** | 2 h |
| 相对最近 `:48` 槽位的延迟 | +0.3 ~ +60.0 min | 0 |

**约 60% 的调度被静默丢弃，且没有一次提前。** 这与 `OPERATIONS.md` 里 09-14~09-16 观测到的"12 次只成功 4-5 次"一致，现在只是样本更长。

对照 Azure Container Apps 的 cron：连续 4 次触发**误差 0 秒**。

所以拆分是：**Azure 负责时钟，GitHub 负责算力**。Azure 侧只剩下这一个容器，做一次 HTTPS 调用就退出。

```
Azure Container Apps Job  polar-trigger   (cron 48 */2 * * *, 0.25 vCPU)
        │  POST /repos/.../actions/workflows/pipeline.yml/dispatches
        ▼
GitHub Actions            Polar Plus Pipeline   (public repo, 分钟数免费)
        │  upload-pages-artifact -> deploy-pages
        ▼
GitHub Pages              https://unknown70022024.github.io/polar_plus/
```

实测这一跳的延迟：**POST 返回 204 用 1.9s，run 在 POST 之后 1.398s 创建并进入 in_progress**。

## 2. 成本

仓库是 public，GitHub 官方计费文档明确写了 public repository 用标准 GitHub-hosted runner **免费**。
所以重活全部落在 $0 的一侧。

Azure 侧只跑触发：

| | 现状（被暂停的全管线任务） | 拆分后（触发任务） |
|---|---:|---:|
| 每次占用 | 2 vCPU | 0.25 vCPU |
| 单次时长 | ~115 s 均值 | 数秒 |
| 每月 vCPU-秒 | ~82,800（**额度的 46%**）| ~900-2,700（**0.5%-1.5%**）|
| 费用 | $0 | $0 |

省下的不是钱，是**额度余量**——余量是提高频率的前提。

> 这个方案的经济性完全建立在"仓库保持 public"上。一旦改成 private，GitHub 的分钟数开始计费，整个账要重算。

## 3. 部署了什么

| 资源 | 名称 | 说明 |
|---|---|---|
| 资源组 | `polar-plus-rg` (eastasia) | 复用 |
| 环境 | `polar-plus-env` | 复用，无新增固定费用 |
| **新任务** | `polar-trigger` | cron `48 */2 * * *`，0.25 vCPU / 0.5 GiB，超时 120s，执行级重试 2 次 |
| 旧任务 | `polar-plus` | **已暂停但完整保留**（见第 5 节）|
| 镜像 | `ghcr.io/unknown70022024/polar-plus-trigger:v1` | 132 MB（管线镜像 696 MB）|

镜像刻意不复用 `azure/Dockerfile`：那个镜像为了跑管线装了 h5py、Pillow 和可选的 Node + SWA CLI。
触发器只需要标准库。

代码：`azure/trigger/trigger.py`、`azure/trigger/Dockerfile`。

### 触发器的退出码就是告警信号

| 退出码 | 含义 | 该怎么办 |
|---:|---|---|
| 0 | GitHub 接受了派发（HTTP 204） | 无需动作 |
| 1 | HTTP 401 | **token 过期或被吊销** —— 换新 PAT 后更新任务密钥 |
| 1 | HTTP 403 | token 权限不足（需要 `Actions: write`）或触发限流 |
| 1 | HTTP 404 | 仓库/工作流不存在，或该 ref 上没有 `workflow_dispatch` |
| 1 | HTTP 422 | ref 不存在 |
| 1 | 无响应 | 容器出不去了 |

429 和 5xx 会自动重试（最多 5 次，指数退避）。

## 4. 构建与推送镜像

```bash
cd <repo root>
podman build -f azure/trigger/Dockerfile -t polar-plus-trigger:v1 \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
podman tag polar-plus-trigger:v1 ghcr.io/unknown70022024/polar-plus-trigger:v1
printf '%s' "$GHCR_PAT" | podman login ghcr.io -u unknown70022024 --password-stdin
podman push ghcr.io/unknown70022024/polar-plus-trigger:v1
```

`BASE_IMAGE` 可覆盖是因为本机到 Docker Hub 不通（`registry-1.docker.io` 超时），镜像源直接用。

> **Dockerfile 里的 `chmod 0644` 是载荷，不是整洁。** `COPY` 保留源文件权限，工作副本的 umask 若是 0600，
> 装进镜像就是 root:root 0600，非 root 的 uid 10001 读不到，运行时报
> `can't open file '/app/trigger.py': [Errno 13] Permission denied`。这个坑在本地容器测试时踩到过。

## 5. 旧任务：暂停与恢复

暂停用的是把触发器类型从 `Schedule` 改成 `Manual`——**不是删除，也不是改 cron**。
`scheduleTriggerConfig` 被清空，镜像、密钥、环境变量、超时全部原样保留，`az containerapp job start` 仍可手动跑。

```bash
SUB=$(az account show --query id -o tsv)
BASE="https://management.azure.com/subscriptions/$SUB/resourceGroups/polar-plus-rg/providers/Microsoft.App/jobs/polar-plus?api-version=2024-03-01"

# 暂停
az rest --method patch --url "$BASE" \
  --body '{"properties":{"configuration":{"triggerType":"Manual","manualTriggerConfig":{"parallelism":1,"replicaCompletionCount":1}}}}'

# 恢复（回到每 2 小时的 :48）
az rest --method patch --url "$BASE" \
  --body '{"properties":{"configuration":{"triggerType":"Schedule","scheduleTriggerConfig":{"cronExpression":"48 */2 * * *","parallelism":1,"replicaCompletionCount":1}}}}'
```

验证暂停是否生效：

```bash
az containerapp job show -n polar-plus -g polar-plus-rg \
  --query "properties.configuration.{trigger:triggerType,cron:scheduleTriggerConfig.cronExpression}" -o json
# 期望 {"cron": null, "trigger": "Manual"}
```

## 6. 包可见性（当前唯一的待办）

Azure 在**创建任务时**就会预先校验镜像可拉取性，私有镜像会直接失败：

```
ERROR: (InvalidParameterValueInContainerTemplate) Field 'template.containers.trigger.image' is invalid
  with details: 'UNAUTHORIZED: authentication required'
```

而 ghcr 的包**没有 REST 接口可以改可见性**——`PATCH /user/packages/...` 返回 404。
`polar-plus` 当初也是照 `DEPLOY.md` 4.4 在网页上手动改的。

**手动步骤（一次，约 10 秒）：**

1. 打开 <https://github.com/users/unknown70022024/packages/container/polar-plus-trigger/settings>
2. 拉到底 **Danger Zone** → **Change visibility** → **Public**

验证是否已是 public（匿名能换到 token 并列出 tags）：

```bash
T=$(curl -s "https://ghcr.io/token?scope=repository:unknown70022024/polar-plus-trigger:pull&service=ghcr.io" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
echo "匿名 token 长度: ${#T}"     # public 约 68，private 为 0
curl -s -H "Authorization: Bearer $T" \
  "https://ghcr.io/v2/unknown70022024/polar-plus-trigger/tags/list"
```

> 裸请求 manifest 返回 **401 是正常的**（OCI 认证挑战），不代表镜像是 private。

**为什么不用 registry 凭据长期绕过**：那样任务就永久依赖一个具备 `read:packages` 的 token。
而 fine-grained PAT 是按仓库授权的，**拿不到 ghcr 包权限**——等于强迫这个 token 永远做 classic。
改成 public 之后，派发用的 token 才能收缩成"只授权 polar_plus、只给 `Actions: write`"。

### 已解决：临时用过 registry 凭据

`polar-trigger` 最初是**带着 registry 凭据**建起来的，因为 Azure 在创建时就校验拉取，
私有镜像根本建不出任务。当时没有增加暴露面——凭据复用的就是派发用的那同一个
secret `gh-dispatch-token`，本来就存在这个任务里。

包改成 public 之后凭据**已经摘掉**，现在 `registries: []`，`secrets` 里只剩
`gh-dispatch-token` 一个。所以轮换 token 时不需要额外维护任何东西，新 token 也不必
是 classic。

确认当前状态（`registries` 应为空）：

```bash
# 确认包已是 public（匿名 token 长度约 68，private 为 0）
T=$(curl -s "https://ghcr.io/token?scope=repository:unknown70022024/polar-plus-trigger:pull&service=ghcr.io" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
echo "匿名 token 长度: ${#T}"

# 摘掉 registry 凭据（secret 保留，派发还要用）
az containerapp job registry remove -n polar-trigger -g polar-plus-rg --server ghcr.io

# 验证：现在应当匿名拉取，任务仍能正常执行
az containerapp job registry list -n polar-trigger -g polar-plus-rg -o table
az containerapp job start -n polar-trigger -g polar-plus-rg
```

`secrets` 里仍然只有 `gh-dispatch-token` 一个——registry 凭据引用的是它，没有第二个密钥。

## 7. 创建触发任务

```bash
az containerapp job create \
  -n polar-trigger -g polar-plus-rg \
  --environment polar-plus-env \
  --trigger-type Schedule \
  --cron-expression "48 */2 * * *" \
  --image ghcr.io/unknown70022024/polar-plus-trigger:v1 \
  --container-name trigger \
  --cpu 0.25 --memory 0.5Gi \
  --replica-timeout 120 \
  --replica-retry-limit 2 \
  --parallelism 1 --replica-completion-count 1 \
  --secrets gh-dispatch-token=<PAT> \
  --env-vars GH_DISPATCH_TOKEN=secretref:gh-dispatch-token
```

> 上面这条命令会把 PAT 放进命令行参数。**要避免的话**：先用占位值建任务，再用文件注入真实值
> （`az rest --method patch --body @file`），然后 `shred` 掉文件。命令行参数对同机其他用户可见。

定时是 UTC，5 段式，`. :48` 是给 NASA 留的 48 分钟写入窗口，与旧任务保持一致。

## 8. 轮换 token

```bash
az containerapp job secret set -n polar-trigger -g polar-plus-rg \
  --secrets gh-dispatch-token=<新 PAT>
```

新 PAT 需要的最小权限：**该仓库的 `Actions: write`**（fine-grained），或 classic 的 `repo`。
不需要 `workflow` 作用域——那个是用来改工作流文件的，不是用来派发的。

> **Container Apps 的密钥对资源组里有 Reader 的人可读**（`az containerapp job show --show-secrets`）。
> 它不比资源本身的 RBAC 更严。这是把 token 放在这里的固有代价。

## 9. 验证

```bash
# 1) 手动执行一次（不等 cron）
az containerapp job start -n polar-trigger -g polar-plus-rg -o table

# 2) 看执行结果
az containerapp job execution list -n polar-trigger -g polar-plus-rg -o table

# 3) 看日志
az containerapp job logs show -n polar-trigger -g polar-plus-rg \
  --container trigger --tail 50 --format text
```

日志期望：

```
[trigger] dispatching unknown70022024/polar_plus :: pipeline.yml @ main
[trigger] accepted in 1.869s (HTTP 204)
[trigger] run: #554 queued https://github.com/.../runs/35321411133
```

## 10. 观测：状态页

现在 Azure 触发任务是**唯一**的调度来源，它坏掉 = 管线停摆。没有配邮件告警
（按你的选择），改成一张网页，把最近 12 条记录摆出来。

**<https://polar-trigger-status.salmonbush-152eb6de.eastasia.azurecontainerapps.io/>**

页面并排显示两个权威来源：

| 列 | 来源 | 回答的问题 |
|---|---|---|
| 一、Azure 时钟 | ARM API 的执行记录 | 时钟到底响了没有 |
| 二、GitHub 管线运行 | GitHub 公开 API | 管线到底跑了没有 |

**并排看才是重点**：Azure 有记录而 GitHub 没有对应 run，说明派发被接受了但 GitHub
没跑起来；Azure 那一列出现缺口，说明时钟本身漏了。只看一边都发现不了。

页面还会算出**相邻执行的间隔**（中位数 / 均值 / 最大值），直接对照 2 小时的设计间隔
——这正是 GitHub 自身 cron 守不住的那个数。

### 它是怎么搭的

| 资源 | 说明 |
|---|---|
| Container App `polar-trigger-status` | 与触发任务**共用同一个镜像**（第二个入口 `status_app.py`，用 `--command` 指定），所以只需要一个包是 public |
| 系统托管身份 | `az containerapp create --system-assigned` |
| 角色 | 该身份在 `polar-trigger` 上有 **Reader**——只够读执行记录 |
| 缩容 | `min-replicas 0`，没人看时缩到零，平时不产生计算费用 |
| 端点 | `/` 页面 · `/data.json` 原始数据 · `/healthz` 探针 |

**这个容器里没有任何凭据**——ARM 用托管身份认证，GitHub 那列读的是公开仓库、无需认证。
这是刻意的：能读执行记录的网页不该同时握着一个能派发的 token。

```bash
az containerapp create -n polar-trigger-status -g polar-plus-rg \
  --environment polar-plus-env \
  --image ghcr.io/unknown70022024/polar-plus-trigger:<tag> \
  --command python --args /app/status_app.py \
  --ingress external --target-port 8080 \
  --min-replicas 0 --max-replicas 1 \
  --cpu 0.25 --memory 0.5Gi \
  --env-vars "SUBSCRIPTION_ID=$SUB" RESOURCE_GROUP=polar-plus-rg \
               JOB_NAME=polar-trigger GH_REPO=unknown70022024/polar_plus \
  --system-assigned

az role assignment create \
  --assignee-object-id "$(az containerapp show -n polar-trigger-status -g polar-plus-rg \
      --query identity.principalId -o tsv)" \
  --assignee-principal-type ServicePrincipal \
  --role Reader \
  --scope "$(az containerapp job show -n polar-trigger -g polar-plus-rg --query id -o tsv)"
```

### 已验证

* 从**保加利亚 / 德国 / 西班牙 / 俄罗斯 / 以色列 / 意大利 / 波兰 / 荷兰**等外部节点
  访问 `/` 与 `/data.json` 均返回 HTTP 200
* 容器日志确认托管身份那条路是通的：
  `[status] azure ok: 4 executions, 3 gaps` 与 `[status] github ok: 12 runs`

### 一个已知的网络限制（重要）

**本机打不开这个页面。** 到 `20.24.241.26:443` 的 TCP 被 RST（80/8080/22 同样），
traceroute 到第 12 跳进入微软网络后即断；而同一台机器访问 Azure Static Web App
（`20.247.40.92`）和 `management.azure.com` 都正常。

因为 check-host 的多个境外节点都能正常打开，**这不是部署问题，是这台机器的网络路径
对那个 IP 的问题**。可能需要在别的网络（手机热点）打开，或者改用镜像方案。

`status_app.py --selftest` 可以在不联网、不需要任何云资源的条件下验证渲染路径
（含降级路径），改动页面后先跑它：

```bash
podman run --rm --entrypoint python <image> /app/status_app.py --selftest
```

## 11. 工作流侧的三处改动

`.github/workflows/pipeline.yml`：

1. **去掉 `schedule:`**，只保留 `workflow_dispatch`。GitHub 不再自己定时。
2. **加 `concurrency`**（`group: polar-pipeline`，`cancel-in-progress: false`）。
   之前没有。两个 run 同时跑会同时去 `deploy-pages` 打架。同组最多保留 1 个运行中 + 1 个等待，
   突发触发会塌缩，不会堆成积压。
3. **exit 2 改成绿色跳过。** 这是最要紧的一处。

### 为什么 exit 2 必须变绿

`polar_plus/run.py` 里 `EXIT_NO_HEALTHY_GCC = 2`，是**故意**非零的：这样后面几步会被跳过，
线上站点不会被空数据覆盖。`azure/entrypoint.sh` 从一开始就是这么处理的（`POLAR_NO_DATA_EXIT=0`），
注释里写得很清楚——"在 GitHub Actions 世界里那个非零码是承重的"。

但 GitHub 侧一直让它失败。后果是一次**完全正常、什么毛病都没有的"无需更新"显示为红色失败**：

```
[ABORT] 没有可搜索的候选：线上已发布的时间戳 2026-09-18 05:00Z 已经覆盖了整个搜索窗口，
        没有更新的文件可发布 —— 本次无需更新
本次不发布，线上数据保持不变。
##[error]Process completed with exit code 2.
```

NASA 落后的时候这种轮次很多（09-16 有 4 次、09-17 有 2 次）。所以**挂 GitHub 失败通知做告警
会被误报淹没，真故障会被埋在噪声里**。

现在：exit 2 → `published=false` + `exit 0`（绿色），后续步骤和 `deploy` job 都靠这个输出门控；
真正非 0 的退出码照旧让 run 变红。

> 改动只在工作流里做，`run.py` 和 `entrypoint.sh` 一行没动——Azure 侧的既有契约不受影响。

## 12. 未处理

* **Azure Static Web App `polar-plus-swa-2026` 会停更。** App 读的是 GitHub Pages
  （`DataUrls.java` 拼 `github.io`），所以不影响线上；但 SWA 从此冻结在最后一版。
  要么删掉，要么在 GitHub workflow 里加一步 SWA 发布（需要把 SWA token 放进 GitHub secrets）。
* **告警尚未配置**（第 10 节）。
