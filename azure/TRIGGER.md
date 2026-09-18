# 定时触发的拆分：Azure 管时钟，GitHub 管算力

> 状态：代码与 Azure 侧配置已就位；**包可见性待改**（见第 6 节）。
> `azure/OPERATIONS.md` 描述的是拆分之前的架构，仍然准确，但不再是当前形态。

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

**为什么不用 registry 凭据绕过**：那样任务就永久依赖一个具备 `read:packages` 的 token。
而 fine-grained PAT 是按仓库授权的，**拿不到 ghcr 包权限**——等于强迫这个 token 永远做 classic。
改成 public 之后，派发用的 token 才能收缩成"只授权 polar_plus、只给 `Actions: write`"。

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

## 10. 告警（唯一的缺口）

现在 Azure 的触发任务是**唯一**的调度来源。它坏掉 = 管线停摆，而且不会有人知道。必须配：

```bash
az monitor action-group create -n polar-alerts -g polar-plus-rg \
  --short-name polar --email-receiver name=me email=<邮箱>

az monitor metrics alert create -n polar-trigger-failed -g polar-plus-rg \
  --scopes "$(az containerapp job show -n polar-trigger -g polar-plus-rg --query id -o tsv)" \
  --condition "count JobExecutionFailed > 0" \
  --window-size 1h --evaluation-frequency 15m \
  --action "$(az monitor action-group show -n polar-alerts -g polar-plus-rg --query id -o tsv)"
```

具体指标名以门户里该任务实际可用的为准。

**告警只盯 Azure 触发任务，不要盯 GitHub 的 run 失败。** 见下一节。

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
