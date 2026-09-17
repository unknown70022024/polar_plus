# Azure 部署详细指南（第一次部署照着做）

从零到管线自动运行。每一步都给了**预期输出**和**出错怎么办**，照着走就行。

预计耗时：45-70 分钟，其中大部分时间在等镜像构建。

---

## 目录

- [0. 先理解你要建的东西](#0-先理解你要建的东西)
- [1. 前置准备](#1-前置准备)
- [2. 选区域（已被订阅策略锁定为 eastasia）](#2-选区域已经被订阅策略锁定为-eastasia)
- [3. 建资源组](#3-建资源组)
- [4. 构建镜像并推到 ghcr.io](#4-构建镜像并推到-ghcrio)
- [5. 建静态网站托管](#5-建静态网站托管)
- [6. 建 Container Apps 环境](#6-建-container-apps-环境)
- [7. 建定时任务](#7-建定时任务)
- [8. 第一次手动运行](#8-第一次手动运行)
- [9. 验证结果](#9-验证结果)
- [10. 日常运维](#10-日常运维)
- [11. 排错速查](#11-排错速查)
- [12. 成本与预算告警](#12-成本与预算告警)
- [13. 改 App 的地址](#13-改-app-的地址)
- [14. 全部删掉](#14-全部删掉)

---

## 0. 先理解你要建的东西

一共 5 个资源，各管一件事：

| 资源 | 干什么 | 类比 |
|---|---|---|
| **资源组** Resource Group | 一个文件夹，装下面所有东西。删它就全删了 | 文件夹 |
| **镜像仓库** ghcr.io | 存放构建好的镜像（GitHub 免费公开包，不是 Azure 资源） | Docker Hub |
| **静态网站** Static Web App | 托管 `root.json` + 6 张瓦片，带 CDN | GitHub Pages |
| **容器环境** Container Apps Environment | 跑容器的"机房"，日志也归它管 | 一台抽象的服务器 |
| **定时任务** Container Apps Job | 每 2 小时启动一次你的镜像，跑完就停 | GitHub Actions 的 schedule |

数据流：

```
ghcr.io（镜像）──拉取──> Container Apps Job（每 2 小时跑一次）
                          │
                          ├─ 从 NASA 下载 GCC 数据
                          ├─ 计算成 6 张瓦片
                          └─ 上传 ──> Static Web App ──> 手机 App
```

**为什么不用虚拟机**：任务每 2 小时只跑约 2 分钟，其余时间不该付钱。容器任务跑完就释放，且每月有免费额度（见第 12 节）。

---

## 1. 前置准备

### 1.1 确认学生订阅可用

浏览器打开 <https://portal.azure.com>，登录你的学生账号。左上角搜索框输入 **Subscriptions（订阅）**，点进去。

预期看到一条订阅，名字类似 **Azure for Students**，状态 **Active（活动）**。

> 如果看到的是 "Pay-As-You-Go" 且没有 $100 额度，说明学生认证没生效，先去 <https://azure.microsoft.com/en-us/free/students/> 重新认证。

### 1.2 安装 Azure CLI

**Windows**（PowerShell，管理员）：
```powershell
winget install -e --id Microsoft.AzureCLI
```

**macOS**：
```bash
brew install azure-cli
```

**Linux（Fedora/RHEL）**：
```bash
sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
sudo dnf install -y https://packages.microsoft.com/config/rhel/9/packages-microsoft-prod.rpm
sudo dnf install -y azure-cli
```

装完**重开一个终端**，然后验证：

```bash
az version
```

预期输出里有 `"azure-cli": "2.7x.x"` 之类。**必须 ≥ 2.79.0**，因为查看任务日志的命令需要这个版本。

> 版本太低的话：`az upgrade`

### 1.3 登录

```bash
az login
```

浏览器会弹出，选你的学生账号登录。终端预期输出一段 JSON，里面有 `"name": "Azure for Students"`。

如果浏览器没弹出（比如在无图形界面的机器上）：
```bash
az login --use-device-code
```
按提示在浏览器里输入终端显示的验证码。

### 1.4 确认当前订阅选对了

```bash
az account show --query "{name:name, id:id, state:state}" -o table
```

预期：
```
Name                Id                                    State
------------------  ------------------------------------  -------
Azure for Students  xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  Enabled
```

> 如果有多个订阅且选错了：
> ```bash
> az account list -o table
> az account set --subscription "Azure for Students"
> ```

### 1.5 安装两个 CLI 扩展

```bash
az extension add --name containerapp --upgrade
```

预期最后一行类似 `The installed extension 'containerapp' is experimental...` 或直接无输出。

> `containerapp` 扩展是**必需的**：创建任务和看日志都靠它。

### 1.6 确认 Azure 能拉 Docker Hub 的镜像

这一步是提前排雷。构建镜像时基础镜像是 `python:3.12-slim`，来自 Docker Hub。

你机器上 **Docker Hub 是被墙的**（我实测 `registry-1.docker.io` 超时），但 **ghcr.io 通**（返回 401，是正常的认证挑战），国内镜像源 `docker.m.daocloud.io` 也通。

所以构建策略是：**本地 podman 构建 → 推 ghcr.io → Azure 直接拉**。基础镜像走国内镜像源绕开 Docker Hub。第 4 节有完整命令。

### 1.7 关于密钥

**你只需要一个密钥：SWA 部署令牌。** 它在第 5 节创建完静态网站后拿。

**SSEC API Key 不需要。** GitHub Actions 上跑的版本从头到尾就没传过这个变量 —— 我核对过 `.github/workflows/pipeline.yml`，里面根本没有 `SSEC_API_KEY`。代码在 key 为空时会直接省略 `accesskey` 参数走匿名访问，实测匿名请求 `re.ssec.wisc.edu` 返回 200，正常工作。

所以容器也按同样方式跑，**第 7 节建任务时不会用到 SSEC**。行为与 GitHub 完全一致。

> 如果你以后想用 key（匿名访问有速率限制，理论上可能被限流），第 7 节末尾有追加方法，不用重建任务。

### 1.8 注册资源提供程序（必做，否则第一个资源就建不出来）

Azure 要求每种资源类型先在订阅上**注册**才能创建。学生订阅通常什么都没注册，所以直接建会报：

```
(MissingSubscriptionRegistration) The subscription is not registered to use
namespace 'Microsoft.Web'.
```

一次注册好这几个（免费、可逆、约 1 分钟）：

```bash
for ns in Microsoft.Web Microsoft.App Microsoft.OperationalInsights \
          Microsoft.Insights Microsoft.Storage; do
  echo "registering $ns"
  az provider register --namespace "$ns" -o none
done

# 等待到全部 Registered（通常 1-2 分钟）
for i in $(seq 1 40); do
  sleep 15
  st=$(for ns in Microsoft.Web Microsoft.App Microsoft.OperationalInsights \
               Microsoft.Insights Microsoft.Storage; do
         az provider show --namespace "$ns" --query registrationState -o tsv
       done | tr '\n' ' ')
  echo "[$((i*15))s] $st"
  case "$st" in *NotRegistered*|*Registering*) ;; *) echo "全部注册完成"; break;; esac
done
```

各提供程序对应的资源：

| 命名空间 | 用于 |
|---|---|
| `Microsoft.Web` | Static Web App |
| `Microsoft.App` | Container Apps 环境与任务 |
| `Microsoft.OperationalInsights` | Log Analytics 工作区（建环境时自动创建）|
| `Microsoft.Insights` | 监控 |
| `Microsoft.Storage` | Blob（只有走 11.1 节才需要，但一并注册无害）|

> 不需要 `Microsoft.ContainerRegistry` —— 我们用 ghcr.io，不用 ACR。

---

## 2. 选区域（已经被订阅策略锁定为 eastasia）

### 2.1 你的订阅只允许 5 个区域

Azure for Students 订阅带一条内置策略 **"Allowed resource deployment regions"**，你的允许列表是：

```
uaenorth   koreacentral   indonesiacentral   centralindia   eastasia
```

往这个列表以外部署会报 `RequestDisallowedByAzure`，而且**学生订阅绕不过去**（唯一出路是升级成 Pay-As-You-Go，那样就没有 $100 额度了）。所以区域不是偏好问题，是硬约束。

### 2.2 和 SWA 的交集：只有 eastasia 一个

Static Web Apps 只在 5 个区域可用，和你的允许列表求交集：

| SWA 支持的区域 | 你的策略允许？ |
|---|---|
| `westus2` | 否 |
| `centralus` | 否 |
| `eastus2` | 否 |
| `westeurope` | 否 |
| **`eastasia`** | **是 —— 唯一交集** |

所以：**全部资源放 `eastasia`（香港）**。这不是我挑的，是唯一可行解。

### 2.3 关于"离 NASA 近一点"

方向是对的，但这个订阅做不到。NASA Langley 在弗吉尼亚州，而你的 5 个允许区域**没有一个是美国**：

| 区域 | 位置 | 到 NASA Langley 大致距离 |
|---|---|---|
| `koreacentral` | 首尔 | ~11,000 km |
| `uaenorth` | 迪拜 | ~11,500 km |
| **`eastasia`** | **香港** | ~13,000 km |
| `centralindia` | 浦那 | ~13,000 km |
| `indonesiacentral` | 雅加达 | ~16,500 km |

既然都远，**选 `eastasia` 反而是最优的**：

1. 它是唯一能放 SWA 的区域，于是容器和 SWA **同区域**，跨区域流量 $0
2. 成熟区域，国际带宽好。`indonesiacentral` 是相当新的区域，Container Apps 覆盖未必齐全
3. 离你自己最近，调试和验证方便

**唯一的不确定性是 eastasia 到 NASA 的下载速度**，这只能第一次运行时实测，见第 8.3 节。

### 2.4 跨区域费用（换用 ghcr.io 后基本不存在）

| 传输类型 | 费率 |
|---|---|
| 互联网 → Azure（**入站**） | **免费**，永远 |
| Azure → 互联网 | 每月前 100 GB 免费，之后 $0.087/GB |
| **同一区域内** | **免费** |
| 跨区域·跨大洲 | ~$0.05-0.08/GB |

全部放 `eastasia`：

| 路径 | 月流量 | 费用 |
|---|---|---|
| NASA → 容器（入站） | ~360 GB | **$0** |
| ghcr.io → 容器（镜像拉取） | 696 MB × 360 次 = 238 GB | **$0**（入站免费 + 公开镜像不收费） |
| 容器 → SWA（发布） | 0.72 GB | **$0**（同区域） |
| SWA → 用户 | 前 100 GB 免费 | **$0** |

> **镜像这一项值得说明**：镜像从 ghcr.io 拉进 Azure 属于**入站**，免费。如果改用 ACR 且 ACR 和容器跨区域，那 238 GB 就是唯一会产生费用的地方（约 $14/月）。这是不用 ACR 的第二个理由（第一个是 $5/月的 SKU 费）。
### 2.5 一次性设好变量

```bash
# ---- 改这里 ----
export RG="polar-plus-rg"          # 资源组名，随便取
export LOC="eastasia"              # 唯一允许且支持 SWA 的区域，别改
export SWA="polar-plus-swa-2026"   # 全局唯一，决定你的网址
export ENV="polar-plus-env"
export JOB="polar-plus"
export IMAGE_TAG="v1"
export GHCR_USER="你的GitHub用户名"  # ghcr.io 镜像地址要用
# ----------------

echo "RG=$RG  LOC=$LOC  SWA=$SWA  GHCR_USER=$GHCR_USER"
```

**注意没有 `ACR` 了** —— 不用 Azure 容器仓库，改用 ghcr.io，省掉 $5/月，也绕开被策略拦截的问题。详见第 4 节。

> **`SWA` 的坑**：必须全局唯一。网址是 `https://<SWA>.azurestaticapps.net`，取个好记的。报错 `already exists` 就换一个。
>
> **区域一致性**：第 5、6 节创建 SWA 和容器环境时都用 `$LOC`，不要单独改某一个的区域。
>
> **建议存成文件**（新开终端变量会丢）：
> ```bash
> cat > ~/polar-azure.sh <<'EOF'
> export RG="polar-plus-rg"
> export LOC="eastasia"
> export SWA="polar-plus-swa-2026"
> export ENV="polar-plus-env"
> export JOB="polar-plus"
> export IMAGE_TAG="v1"
> export GHCR_USER="你的GitHub用户名"
> EOF
> ```
> 之后每次开工 `source ~/polar-azure.sh`。

### 2.6 开工自检（每次开始操作前跑一遍）

`export` 只是当前终端的变量，新开终端就没了；而且模板里的占位符很容易忘了替换 —— 那种错误会一路带到推送镜像才爆出来。所以每次动手前跑这个：

```bash
# 把预期的值填进来（GHCR_USER 特别容易忘）
EXPECT_GHCR_USER="unknown70022024"

fail=0
for v in RG LOC SWA ENV JOB IMAGE_TAG; do
  eval "val=\$$v"
  if [ -z "$val" ]; then echo "缺失: \$$v"; fail=1; fi
done
[ "$GHCR_USER" != "$EXPECT_GHCR_USER" ] && { echo "请设置 GHCR_USER=$EXPECT_GHCR_USER（当前：'$GHCR_USER'）"; fail=1; }
[ -z "$SWA_TOKEN" ] && echo "提示: SWA_TOKEN 还没设（第 5.2 节取）"
[ "$fail" -eq 0 ] && echo "✓ 变量就绪：$RG / $LOC / $SWA / $GHCR_USER"

# 确认订阅没选错
az account show --query "{name:name, state:state}" -o table
```

预期最后看到 `✓ 变量就绪：...`，以及订阅是 `Azure for Students` / `Enabled`。

---

## 3. 建资源组

```bash
az group create --name "$RG" --location "$LOC" -o table
```

预期：
```
Location    Name
----------  --------------
eastasia    polar-plus-rg
```

> 报错 `RequestDisallowedByAzure` 或 `LocationNotAvailableForResourceType`：区域不在订阅允许列表里，换回 `eastasia`（第 2.1 节）。
>
> 报错 `AuthorizationFailed`：订阅没选对，回第 1.4 节。

---

## 4. 构建镜像并推到 ghcr.io

**不用 Azure Container Registry。** 两个原因：

1. ACR 没有免费档，Basic 约 **$5/月**
2. ACR 是被订阅策略拦下的那个资源（你刚踩到的 `RequestDisallowedByAzure`）

改用 **GitHub Container Registry（ghcr.io）**。GitHub 官方计费文档明确写了：

> **"GitHub Packages usage is free for public packages."** —— 公开包完全免费，存储和流量都不收费。

而且**公开镜像拉取不需要任何凭据**，Container Apps 直接拉，连 `--registry-server` 都不用配。

### 4.1 准备 GitHub PAT

需要一个有 `write:packages` 权限的 Personal Access Token：

1. <https://github.com/settings/tokens> → **Generate new token (classic)**
2. 勾选 **`write:packages`**（勾它会自动带上 `read:packages`）
3. 生成后复制，存进变量：

```bash
export GHCR_PAT="ghp_你的token"
```

### 4.2 本地构建

你机器上 Docker Hub 是被墙的（`registry-1.docker.io` 超时），但 ghcr.io 和国内镜像源都通。所以：

```bash
cd ~/polar_plus          # 换成你的实际仓库路径（就是含有 azure/ 和 polar_plus/ 的那一层）

# 确认在仓库根目录：下面两个目录名都应该打印出来
ls -d polar_plus azure

# 先确认变量还在（新开终端会丢）。${VAR:?消息} 会在变量为空时直接报出消息，
# 而不是让 podman 抛一句难懂的 "invalid reference format"
export GHCR_USER="${GHCR_USER:?请先 export GHCR_USER}"
export IMAGE_TAG="${IMAGE_TAG:?请先 export IMAGE_TAG}"
echo "将构建：ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"

podman build -f azure/Dockerfile \
  -t "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  .
```

> **镜像地址能直接告诉你哪个变量空了**：
>
> | tag 长这样 | 哪个变量为空 |
> |---|---|
> | `ghcr.io//polar-plus:v1` | `GHCR_USER`（双斜杠） |
> | `ghcr.io/user/polar-plus:` | `IMAGE_TAG`（冒号后没东西） |
> | `ghcr.io//polar-plus:` | 两个都空 |

**这个过程要 8-12 分钟**（装 Python 依赖 + Node + 编译 SWA CLI）。最后看到 `Successfully tagged ...` 就成了。

> `BASE_IMAGE` 参数就是为墙内环境准备的，指向国内镜像源。Azure 上不需要，默认值就是官方镜像。
>
> 用 `docker` 而不是 `podman` 也可以，命令一样。

**中间会看到这些，全都是无害的，不要中断：**

```
npm WARN deprecated rimraf@2.7.1: ...            ← 依赖库自己的弃用警告
npm WARN tar TAR_ENTRY_ERROR EINVAL: ... fchown  ← rootless podman 的已知现象
npm WARN deprecated sudo-prompt@8.2.5: ...
RUN npm cache clean --force
npm WARN using --force Recommended protections disabled.
```

**还有一条看起来像报错但其实不是：**

```
npm ERR! prebuild-install warn install Request timed out
```

这是 SWA CLI 的原生模块 `keytar` 在尝试从 GitHub releases 下载预编译二进制，在墙内通常会超时。**超时之后会自动回退到从源码编译，而 Dockerfile 已经装好了编译所需的全部依赖**，所以会继续往下走并最终成功。这个过程会白等 30-60 秒，属正常。

> 如果这里**最终失败了**（`npm ERR! not ok` 之类），说明源码编译也没过。第 11 节排错表里有对应条目。

### 4.3 推送到 ghcr.io

```bash
echo "$GHCR_PAT" | podman login ghcr.io -u "$GHCR_USER" --password-stdin
podman push "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"
```

推送大约 696 MB，视上行带宽需要几分钟。

### 4.4 把镜像设为 public（重要）

**默认可能是 private，private 就要算存储和流量费了。** 推完之后：

1. 打开 `https://github.com/users/<你的用户名>/packages/container/polar-plus`
2. 右侧 **Package settings** → 拉到底 **Danger Zone**
3. **Change visibility** → 选 **Public** → 确认

如果仓库本身是 public 的，package 有时会自动继承 public，但仍然去确认一下。

### 4.5 验证镜像可匿名拉取

**先看一个反直觉的点**：直接请求 manifest **一定返回 401，即使镜像是 public 的**。

这是 OCI registry 的**认证挑战**机制，不是"私有"的标志。401 响应里带的 `WWW-Authenticate` 头是在告诉你"去这个地址换 token"。客户端（podman/docker）会自动完成这个流程，所以平时你感觉不到。**对 public 包，token 可以匿名换取；对 private 包，匿名换不到带 pull 权限的 token。**

所以正确的验证是两步：

```bash
# 1) 匿名换 token
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:$GHCR_USER/polar-plus:pull&service=ghcr.io" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
echo "token 长度: ${#TOKEN}"

# 2) 用 token 列出 tags —— 能列出来就说明 public
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://ghcr.io/v2/$GHCR_USER/polar-plus/tags/list"
```

预期：

```
token 长度: 68
{"name":"unknown70022024/polar-plus","tags":["v1"]}
```

| 结果 | 含义 |
|---|---|
| 拿到 token 且列出 tags（含你的 `$IMAGE_TAG`） | **public，配置正确，继续** |
| token 为空 / 极短，或 tags 返回 401 / `UNAUTHORIZED` | 还是 private，回 4.4 |

> **如果你想直接确认 manifest 也能取到**（可选，注意要带全 `Accept` 类型 —— podman 默认推的是 **OCI** manifest，只写 Docker v2 类型会得到 404）：
> ```bash
> curl -s -o /dev/null -w "HTTP %{http_code}\n" \
>   -H "Authorization: Bearer $TOKEN" \
>   -H "Accept: application/vnd.oci.image.manifest.v1+json" \
>   -H "Accept: application/vnd.oci.image.index.v1+json" \
>   -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
>   "https://ghcr.io/v2/$GHCR_USER/polar-plus/manifests/$IMAGE_TAG"
> ```
> 预期 `HTTP 200`。

第 7 节建任务时**不需要**任何 registry 凭据，就靠这一步。如果这里是 private，任务第一次执行会报镜像拉取失败。

---


## 5. 建静态网站托管

### 5.1 创建

```bash
az staticwebapp create \
  --name "$SWA" \
  --resource-group "$RG" \
  --location "$LOC" \
  --sku Free \
  -o table
```

预期输出包含 `defaultHostname`，形如 `<SWA>.azurestaticapps.net`。

> **如果报错说需要 `--source`**（部分 CLI 版本要求指定代码来源）：去门户建。
> 门户 → 创建资源 → 搜 "Static Web App" → **部署来源选「其他」（Other）** → 区域选 East Asia → 计划类型选 **Free** → 创建。
> 建完回来继续 5.2。

### 5.2 记下网址和部署令牌

```bash
export SWA_HOST=$(az staticwebapp show -n "$SWA" -g "$RG" \
  --query defaultHostname -o tsv)
export SWA_TOKEN=$(az staticwebapp secrets list -n "$SWA" -g "$RG" \
  --query properties.apiKey -o tsv)

echo "网址   : https://$SWA_HOST"
echo "令牌长度: ${#SWA_TOKEN}"
```

预期：第一行是你的网址；第二行的令牌长度应该在 **100 以上**（是一长串字符）。

> **令牌泄露了怎么办**：门户 → 你的 Static Web App → 概述 → 管理部署令牌 → 重置。重置后要更新第 7 节的任务配置。

---

## 6. 建 Container Apps 环境

这是跑容器的"机房"，同时自动创建一个 Log Analytics 工作区收日志。

```bash
az containerapp env create \
  --name "$ENV" \
  --resource-group "$RG" \
  --location "$LOC" \
  --environment-mode ConsumptionOnly
```

**`--environment-mode ConsumptionOnly` 不能省。** 新版 CLI 默认创建的是 **Express** 环境，而 Express **不支持 Job** —— 不写这个参数，下一步建任务会直接失败：

```
(ExpressEnvironmentResourceNotSupported) 'Job' resources are not supported
on express environments.
```

环境模式三选一：

| 模式 | 能用 Job 吗 | 说明 |
|---|---|---|
| `Express`（**CLI 默认**） | 否 | 快速创建的精简环境，只支持长期运行的 app |
| `ConsumptionOnly` | **是** | 经典无服务器模式，单副本上限 2 核 / 4 GiB，我们用 1 核 / 2 GiB，够 |
| `WorkloadProfiles` | 是 | 支持更大规格，但用了 Dedicated 配置文件会有固定管理费 |

选 `ConsumptionOnly`：满足需求，且纯按量计费（走第 12 节的免费额度）。

**这个过程要 3-6 分钟**，终端会显示进度。输出类似：

```
WARNING: No Log Analytics workspace provided.
WARNING: Generating a Log Analytics workspace with name "workspace-polarplusrgXXXX"
...
Location    Name              ResourceGroup
----------  ----------------  ---------------
East Asia   polar-plus-env    polar-plus-rg
```

记下自动生成的**日志工作区名字**（第 12 节设每日上限要用）。

验证（**关键是看 `mode`，必须是 `ConsumptionOnly` 或 `WorkloadProfiles`，不能是 `Express`**）：

```bash
az containerapp env show -n "$ENV" -g "$RG" \
  --query "{name:name, location:location, state:properties.provisioningState, mode:properties.environmentMode}" -o table
```

预期：

```
Name            Location    State      Mode
--------------  ----------  ---------  ----------------
polar-plus-env  East Asia   Succeeded  ConsumptionOnly
```

> 如果已经建成了 Express，删掉重建即可：
> ```bash
> az containerapp env delete -n "$ENV" -g "$RG" --yes
> # 然后重新执行上面的 create，带上 --environment-mode ConsumptionOnly
> ```

### 6.1 确认两个资源真的都在 eastasia

### 6.1 确认两个资源真的都在 eastasia

这一步别跳过。资源如果落到允许列表以外的区域会直接报错，但万一落到了允许列表内的**另一个**区域（比如 `koreacentral`），容器和 SWA 就跨区域了 —— 发布流量只有 0.72 GB/月，费用可以忽略，但排错时会更乱。确认一下更省心。

```bash
az resource list -g "$RG" \
  --query "[].{name:name, type:type, location:location}" -o table
```

预期（**`location` 那一列必须全是 `eastasia`**）：

```
Name                  Type                                      Location
--------------------  ----------------------------------------  ----------
polar-plus-swa-2026   Microsoft.Web/staticSites                 eastasia
polar-plus-env        Microsoft.App/managedEnvironments         eastasia
<自动创建的日志工作区>  Microsoft.OperationalInsights/workspaces  eastasia
```

**注意这里没有 ACR** —— 镜像在 ghcr.io 上，不属于这个资源组。如果有任何一行的 `location` 不是 `eastasia`，**现在删掉重建**，别往下走：

```bash
# 单独删掉那个放错的资源，然后回到对应章节重建
az resource delete --ids "<上面表格里那一行的资源 ID>"
```

> 查资源 ID：
> ```bash
> az resource list -g "$RG" --query "[].{name:name, id:id, location:location}" -o table
> ```

> **为什么 SWA 这一行最关键**：SWA 只在 5 个区域可用，而你的订阅只允许其中 1 个（`eastasia`）。放错了会直接创建失败。

---

## 7. 建定时任务

先确认令牌还在（新开的终端会丢）：

```bash
[ -n "$SWA_TOKEN" ] && echo "SWA token: ${#SWA_TOKEN} 字符" || echo "错误：SWA_TOKEN 为空，请回第 5.2 节"
```

为空的话**不要往下走**，否则发布步骤会失败。

一次性把任务建出来。**注意 `--memory` 必须带 `Gi` 后缀**（`4Gi`，不是 `4G`）。

```bash
az containerapp job create \
  --name "$JOB" \
  --resource-group "$RG" \
  --environment "$ENV" \
  --trigger-type Schedule \
  --cron-expression "48 */2 * * *" \
  --container-name polar-plus \
  --image "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --cpu 2 --memory 4Gi \
  --replica-timeout 1800 \
  --replica-retry-limit 0 \
  --parallelism 1 --replica-completion-count 1 \
  --secrets swa-token="$SWA_TOKEN" \
  --env-vars \
      POLAR_PUBLIC_BASE_URL="https://$SWA_HOST" \
      POLAR_PUBLISH_TARGET=swa \
      OUTPUT_DIR=/data/output \
      SWA_DEPLOYMENT_TOKEN=secretref:swa-token
```

**注意两点**：

1. **没有 `SSEC_API_KEY`** —— 和 GitHub Actions 上的行为一致，匿名访问 SSEC 即可
2. **没有任何 registry 参数** —— 镜像在 ghcr.io 上且是 public，拉取不需要凭据。这也是第 4.4 / 4.5 节必须确认镜像为 public 的原因：如果不是 public，这一步建的任务会在第一次执行时报镜像拉取失败

### 7.1 以后想补上 SSEC key（可选）

不用重建任务，两步即可：

```bash
az containerapp job secret set -n "$JOB" -g "$RG" --secrets ssec-key="你的key"

# --env-vars 是整体替换，所以要把现有的全部再列一遍
az containerapp job update -n "$JOB" -g "$RG" \
  --env-vars \
      POLAR_PUBLIC_BASE_URL="https://$SWA_HOST" \
      POLAR_PUBLISH_TARGET=swa \
      OUTPUT_DIR=/data/output \
      SWA_DEPLOYMENT_TOKEN=secretref:swa-token \
      SSEC_API_KEY=secretref:ssec-key
```

### 逐个参数解释

| 参数 | 含义 |
|---|---|
| `--cron-expression "48 */2 * * *"` | 每 2 小时的第 48 分钟跑一次（UTC）。`:48` 是给 NASA 的 GCC 文件留 48 分钟写完 |
| `--cpu 2 --memory 4Gi` | **必须 4 GiB，不要用 2 GiB。** 读取阶段 `bt`、`cloud_phase`、`density` 三个 float32 (6480×12960) 数组各 336 MB，同时驻留就已超过 1 GB，再加上单带缓冲、LANCZOS 重采样中间量和 SSEC 墨卡托图，峰值会顶到 2 GiB 以上 —— 实测在 2 GiB 下会被 OOM kill，日志停在密度统计那一行且没有任何 Python 异常。4 GiB 是 ConsumptionOnly 模式的上限，够用 |
| `--replica-timeout 1800` | 单次执行最多 30 分钟，超时杀掉 |
| `--replica-retry-limit 0` | 失败不重试。因为退出码 2 表示"没有健康数据"，重试没意义 |
| `--secrets` | 两个密钥，下面用 `secretref:` 引用，不会明文出现在任务配置里 |
| `POLAR_PUBLIC_BASE_URL` | 发布到哪，也用来读回线上的 `root.json`（回退下界靠它） |
| `POLAR_PUBLISH_TARGET=swa` | 发布到 Static Web App |

验证：

```bash
az containerapp job show -n "$JOB" -g "$RG" \
  --query "{name:name, trigger:properties.configuration.triggerType, cron:properties.configuration.scheduleTriggerConfig.cronExpression, state:properties.provisioningState}" -o table
```

预期：
```
Name        Trigger    Cron            State
----------  ---------  --------------  ---------
polar-plus  Schedule   48 */2 * * *    Succeeded
```

---

## 8. 第一次手动运行

**不要等定时触发**，手动跑一次，这样能立刻看到问题。

```bash
az containerapp job start -n "$JOB" -g "$RG" -o table
```

预期输出里有 `Name` 一列，形如 `polar-plus-abc12de`，这是**执行 ID**。记下它。

### 8.1 等待并查看状态

```bash
# 每 30 秒查一次，或直接重复执行这条
az containerapp job execution list -n "$JOB" -g "$RG" \
  --query "[0].{name:name, status:properties.status, start:properties.startTime, end:properties.endTime}" -o table
```

`status` 会经历 `Running` → `Succeeded`（或 `Failed`）。

**第一次运行要 5-20 分钟**，取决于从 NASA 下载的速度。耐心等。

> **如果卡在 Running 超过 30 分钟**：会被 `--replica-timeout 1800` 杀掉，然后你可以从日志看出卡在哪。

### 8.2 看日志（最关键的一步）

```bash
az containerapp job logs show \
  -n "$JOB" -g "$RG" \
  --container polar-plus \
  --tail 100 \
  --format text
```

> 第一次运行这个命令会自动安装 `containerapp` 扩展，稍等几秒。

### 预期看到的日志（重点看这几行）

**正常发布的情况：**
```
[2026-xx-xxTxx:48:0xZ] polar_plus pipeline container
[2026-xx-xxTxx:48:0xZ]   publish to : swa
[2026-xx-xxTxx:48:0xZ]   site root  : https://xxx.azurestaticapps.net (from POLAR_PUBLIC_BASE_URL)
[2026-xx-xxTxx:48:0xZ] [1/4] NASA GCC cloud composite -> cubemap
[0/5] 回退下界（线上已发布的时间戳）: ...
[1/5] Loading GCC v2a global cloud composite...
     BT_10.8um 在第 0 带（lat 90..45N）: 无效率 x.x%, 死块 0, 累计 0
     ...
  Density: (2500, 5000), zeros=xx.x%
[2026-xx-xxTxx:5x:xxZ] [2/4] Blitzortung lightning
[2026-xx-xxTxx:5x:xxZ] [3/4] NOAA OVATION aurora
[2026-xx-xxTxx:5x:xxZ] [4/4] publishing (2026xxxx_xxxxxx)
[2026-xx-xxTxx:5x:xxZ] done: published 2026xxxx_xxxxxx
```

**看到 `done: published` 就是成功了。**

**"没有健康数据"的情况（这是正常的，不是故障）：**
```
[1/4] no complete healthy GCC file this round.
      Nothing was produced and nothing will be published;
      the live site keeps serving its previous version.
```
这说明 NASA 最近几小时的文件都有问题，门禁拦住了。**线上数据保持不变**，等 NASA 出好文件会自动恢复。

**真正失败的情况**：看到 `FAILED with exit code 1` 或 Python traceback。对照第 11 节排错。

### 8.3 实测 eastasia 到 NASA 的下载速度

**这是整个方案唯一没有验证过的环节。** `eastasia` 是被订阅策略逼出来的选择，不是因为它离 NASA 近 —— 香港到弗吉尼亚约 13,000 km。我没法在部署前实测这条链路，第一次运行就能量出来，看这两行：

```
[1/5] Loading GCC v2a global cloud composite...
     BT_10.8um 读毕 (6480, 12960) in XXXs, ...
  Time: XXXs
```

`Time:` 那一行是**第 1 步（下载 + 解析）的总耗时**，是整个管线里唯一可能耗时的部分。参照值：

| 第 1 步耗时 | 判断 |
|---|---|
| < 90 秒 | 很好，比 GitHub Actions 还快 |
| 90-300 秒 | 正常，可接受 |
| 300-1200 秒 | 偏慢，但 30 分钟超时还够用 |
| 接近或超过 1800 秒 | 会撞上 `--replica-timeout` 被杀，需要放宽超时（见下） |

对比基准：这台开发机实测 **0.10 Mbit/s**（1 GB 要 22 小时，完全不可用）；GitHub Actions 整个管线 1.4-2.3 分钟，说明它的出口约 **90 Mbit/s**。

**如果偏慢，能做的调整有限**，因为 5 个允许区域全在亚洲/中东，没有一个是美国：

1. **先放宽超时**，这是最简单的：`az containerapp job update -n "$JOB" -g "$RG" --replica-timeout 5400`（90 分钟）
2. **换允许列表里的另一个区域**试试（`koreacentral` 首尔最近，约 11,000 km）：
   ```bash
   az group delete --name "$RG" --yes --no-wait   # 注意：SWA 也要重建
   ```
   但 **SWA 必须留在 `eastasia`**（唯一支持的区域），所以换区域会让容器和 SWA 跨区域 —— 发布流量 0.72 GB/月，跨大洲约 $0.05/月，可以忽略。
3. 如果慢到不可用（比如低于 1 Mbit/s），那就只能考虑脱离学生订阅，或者回到 GitHub Actions 定时触发。

**建议第一次手动执行时就盯着这个数字。** 它决定这个方案能不能长期跑下去。

---

## 9. 验证结果

### 9.1 检查线上文件

```bash
curl -s "https://$SWA_HOST/root.json"
```

预期（**关键是 `baseUrl` 指向你的 SWA，且带 `gate` 标记**）：
```json
{"baseUrl": "https://xxx.azurestaticapps.net/tiles/", "timestamp": "20260916_120000", "gate": 1}
```

> **如果返回 404**：说明首次运行还没成功发布过。回第 8.2 节看日志。
>
> **如果 `baseUrl` 里是 `localhost` 或 `owner.github.io`**：说明 `POLAR_PUBLIC_BASE_URL` 没生效。检查第 7 节的 `--env-vars`。发布器本来会拦住这种占位地址不让上线，所以正常情况下不会出现。

### 9.2 检查瓦片

```bash
curl -sI "https://$SWA_HOST/tiles/px.jpg" | head -3
```

预期 `HTTP/2 200`。

### 9.3 检查另外两个数据文件

```bash
curl -s -o /dev/null -w "storms.json: %{http_code}\n" "https://$SWA_HOST/storms.json"
curl -s -o /dev/null -w "aurora.json: %{http_code}\n" "https://$SWA_HOST/aurora.json"
```

预期两个都是 `200`。

### 9.4 肉眼看一下瓦片

浏览器打开 `https://<你的>.azurestaticapps.net/tiles/pz.jpg`（北极面）和 `nz.jpg`（南极面）。

**预期**：正常的云图，极地没有大片黑边，没有明显的接缝黑线。

---

## 10. 日常运维

### 手动触发一次

```bash
az containerapp job start -n "$JOB" -g "$RG"
```

### 看最近几次执行

```bash
az containerapp job execution list -n "$JOB" -g "$RG" \
  --query "[].{name:name, status:properties.status, start:properties.startTime}" -o table
```

保留最近 100 次成功 + 100 次失败。

### 实时跟日志

```bash
az containerapp job logs show -n "$JOB" -g "$RG" \
  --container polar-plus --follow --tail 30 --format text
```

### 改了代码之后重新部署

```bash
cd ~/polar_plus          # 仓库根目录
export IMAGE_TAG="v2"        # 每次改代码换个新 tag，别复用

podman build -f azure/Dockerfile \
  -t "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .

podman push "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"

az containerapp job update -n "$JOB" -g "$RG" \
  --image "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"
```

> **一定要换新 tag**。容器平台会缓存镜像，复用同一个 tag 可能导致更新不生效。

### 改定时频率

改 cron 表达式（UTC，5 段：分 时 日 月 周）：

```bash
az containerapp job update -n "$JOB" -g "$RG" \
  --cron-expression "48 */1 * * *"     # 改成每小时
```

### 改环境变量

```bash
az containerapp job update -n "$JOB" -g "$RG" \
  --set-env-vars POLAR_NO_DATA_EXIT=2      # 例：让"无健康数据"显示为失败
```

> **`create` 和 `update` 用的参数名不一样，这是个陷阱：**
>
> | 命令 | 参数 | 行为 |
> |---|---|---|
> | `az containerapp job create` | `--env-vars` | 全量设置 |
> | `az containerapp job update` | `--set-env-vars` | 追加/覆盖单个，**推荐** |
> | `az containerapp job update` | `--replace-env-vars` | 整体替换，其他全丢 |
> | `az containerapp job update` | `--remove-env-vars` | 删除指定变量 |
>
> **在 `update` 上写 `--env-vars` 不会报错，会被静默忽略** —— 你以为改了，其实没改。用 `--set-env-vars` 最安全，它是增量的。

---

## 11. 排错速查

| 现象 | 原因 | 解决 |
|---|---|---|
| `RequestDisallowedByAzure` | 部署到了订阅允许列表以外的区域 | 只能用 `uaenorth`/`koreacentral`/`indonesiacentral`/`centralindia`/`eastasia`（第 2.1 节） |
| 建 SWA 报 policy violation 或不支持该区域 | SWA 只在上面的 `eastasia` 可用 | 区域必须是 `eastasia` |
| `LocationNotAvailableForResourceType` | 区域不支持该服务 | SWA 只能用 `westus2`/`centralus`/`eastus2`/`westeurope`/`eastasia` |
| 创建 SWA 报 policy violation | SWA 只在你允许列表里的 `eastasia` 可用 | 区域必须是 `eastasia` |
| 任务执行报镜像拉取失败 `UNAUTHORIZED`/`MANIFEST_UNKNOWN`/`DENIED` | ghcr.io 镜像是 private，或 tag 写错 | 回第 4.4 节把 package 改成 public；用第 4.5 节的两步命令确认能匿名列出 tags |
| 用 curl 查 ghcr 镜像返回 **401** | **这是正常的认证挑战，不代表镜像是 private** | 走第 4.5 节：先匿名换 token，再用 token 请求。裸请求 401 是预期行为 |
| `az policy assignment list` 返回空或权限不足 | 学生订阅不让读策略 | 用门户看：Policy → Authoring → Assignments（第 2.1 节）|
| 建任务报 `ExpressEnvironmentResourceNotSupported` | 容器环境是 CLI 默认的 **Express** 模式，不支持 Job | 删掉环境重建，create 时加 `--environment-mode ConsumptionOnly`（第 6 节）|
| 建第一个资源就报 `MissingSubscriptionRegistration ... namespace 'Microsoft.Web'` | 订阅没注册该资源类型（学生订阅默认全未注册）| 跑第 1.8 节注册 `Microsoft.Web` / `Microsoft.App` / `Microsoft.OperationalInsights` / `Microsoft.Insights` |
| 发布时报 `Failed to find a default file in the app artifacts folder` | `swa deploy` 要求目录里有 `index.html` | `publish.py` 已自动生成 `index.html`（和 `staticwebapp.config.json` 一起，且在上传列表生成**之前**写入）。确认镜像是 v3 或更新的 |
| 任务日志在密度统计那行**戛然而止**，没有 Python 异常 | **OOM kill** —— 读取阶段 `bt`/`cloud_phase`/`density` 三个 float32 数组各 336 MB | 用 `--cpu 2 --memory 4Gi`，不要用 2 GiB（第 7 节）|
| 构建卡在 `npm ERR! ModuleNotFoundError: No module named 'gyp'` | 镜像里的 Python 在 `/usr/local`，看不到 Debian `gyp` 包装的 `/usr/lib/python3/dist-packages` | Dockerfile 已内置 `pip install gyp-next` 修掉；确认你用的是最新版 `azure/Dockerfile` |
| 构建时 `prebuild-install ... Request timed out` | keytar 下载预编译二进制超时（墙内常见） | **正常**，会自动回退源码编译。只要最后有 `Successfully tagged` 就没问题 |
| 不想装 Node / 镜像太大 / SWA 编译反复出问题 | — | 改用 Blob 托管：`--build-arg INSTALL_SWA_CLI=0`（镜像 286 MB 而非 696 MB），发布改用 `--target blob`，见第 11.1 节 |
| 任务日志里 `set POLAR_PUBLIC_BASE_URL` 或 baseUrl 是 localhost | 环境变量没传进去 | 检查第 7 节的 `--env-vars`（create 时）|
| 用 `job update --env-vars` 改变量**没生效且没报错** | `update` 不认 `--env-vars`，静默忽略 | 改用 `--set-env-vars`（第 10 节）|
| `set SWA_DEPLOYMENT_TOKEN ... to use the swa target` | 令牌没传或已失效 | 重新取令牌（第 5.2），更新任务 |
| 日志里 `connection timeout to host satcorps.larc.nasa.gov` | NASA 不可达（常见，会间歇发生） | 等下一次定时运行。`replica-retry-limit 0` 意味着这次就放弃，线上数据不变 |
| 日志里 `no complete healthy GCC file this round` | 门禁判定 NASA 近期文件都不完整 | **正常**。线上保留上一版，等好文件自动恢复 |
| 站点 404，但日志说 published | SWA 传播延迟或发布到了别处 | 等 1-2 分钟重试；检查 `POLAR_PUBLIC_BASE_URL` 是否和 `SWA_HOST` 一致 |
| `az containerapp job logs show` 报未知命令 | CLI 版本 < 2.79 或缺扩展 | `az upgrade` + `az extension add --name containerapp --upgrade` |
| 首次执行后 `root.json` 仍 404 | 首次运行遇到"无健康数据"（见下） | 见下方说明 |

### 11.1 改用 Blob 托管（可选，避开 Node 依赖）

SWA 需要在镜像里装 Node + 编译原生模块，是最容易出问题的一环。如果你不想折腾，或者镜像 696 MB 太大，可以改用 **Azure Blob Storage 静态网站** —— `publish.py` 早就支持这个目标，**代码一行都不用改**。

**取舍：**

| | Static Web Apps Free | Blob 静态网站 |
|---|---|---|
| 费用 | $0 | 约 $0.01/月（2 MB 存储） |
| 带宽 | 100 GB/月，**超出直接停服** | 前 100 GB/月免费，超出 $0.087/GB（**不会停**） |
| CDN | 自带 | 无 |
| 自定义域名 + HTTPS | 免费 | 需 CDN/Front Door（要花钱） |
| 镜像大小 | 696 MB | **286 MB** |
| 构建时间 | 8-12 分钟 | 3-4 分钟 |
| 构建风险 | 要编译原生模块 | 无 |

**对你 10 个用户来说，CDN 带来的差别很小** —— Blob 和 SWA 都在 `eastasia`，香港离你的用户已经很近。

**改法：**

```bash
# 1. 存储账户名必须全局唯一、3-24 位、只能小写字母和数字
export STORAGE="polarplusstorage2026"

az storage account create \
  --name "$STORAGE" --resource-group "$RG" --location "$LOC" \
  --sku Standard_LRS --kind StorageV2 --allow-blob-public-access true

# 2. 开启静态网站托管（内容放在 $web 容器）
az storage blob service-properties update \
  --account-name "$STORAGE" --static-website \
  --index-document index.html --404-document 404.html

export STORAGE_CONN=$(az storage account show-connection-string \
  -n "$STORAGE" -g "$RG" --query connectionString -o tsv)

# 3. 镜像改成本地/Blob 版（不含 Node）
podman build -f azure/Dockerfile \
  -t "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  --build-arg INSTALL_SWA_CLI=0 .
podman push "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"
```

任务的环境变量改成：

```bash
      POLAR_PUBLIC_BASE_URL="https://$STORAGE.z13.web.core.windows.net" \
      POLAR_PUBLISH_TARGET=blob \
      AZURE_STORAGE_CONNECTION_STRING=secretref:storage-conn \
      AZURE_STORAGE_CONTAINER='$web' \
      OUTPUT_DIR=/data/output
```

密钥：`--secrets storage-conn="$STORAGE_CONN"`。

> **`$STORAGE.z13.web.core.windows.net` 里的 `z13` 是区域代码**，`eastasia` 不一定是 13。建完之后用这条拿准确地址：
> ```bash
> az storage account show -n "$STORAGE" -g "$RG" \
>   --query "primaryEndpoints.web" -o tsv
> ```
> 用它的输出（去掉结尾斜杠）作为 `POLAR_PUBLIC_BASE_URL`。

> **第 5 节和第 7 节的 SWA 那部分整个跳过**，其余流程（第 8 节首次运行、第 9 节验证、第 10 节运维）完全一样。


### 搜索窗口（48 小时）

管线从**最近的 UTC 整点**（`MIN_AGE_HOURS` = 2 小时之前，更年轻的文件还在写）开始逐小时向前找，取第一个通过了完整性门禁的文件，最多向前找 **48 小时**。

窗口不是越窄越好。2026-09-15/16 那两天，NASA 的 15:00Z、14:00Z 以及 09-16 的多个整点全是损坏的 —— 窗口太窄会导致**整整一天什么都发布不出去**，而健康文件（09-15 13:00Z）就在窗口外面一点。

想临时调整（不用重建镜像）：

```bash
# 放宽到 96 小时跑一次（用 --set-env-vars，增量修改，不会动其他变量）
az containerapp job update -n "$JOB" -g "$RG" \
  --set-env-vars POLAR_SEARCH_HOURS=96
```

> 改回默认值：`--remove-env-vars POLAR_SEARCH_HOURS`。

### root.json 不存在时

全新站点上 `root.json` 是 404，读不到"已发布的时间戳"，所以没有回退下界 —— 此时唯一的限制就是那 48 小时窗口。这是设计行为：没有线上数据就没有"不能发布更旧数据"的约束。

---

## 12. 成本与预算告警

### 预期的账单

| 项目 | 用量 | 费用 |
|---|---|---|
| Container Apps 任务 | 2 vCPU/4GiB × 约 180-420 秒 × 12 次/天 | **$0 ~ $3.7/月**（见下）|
| 镜像仓库 ghcr.io | 公开包 | **$0**（GitHub 官方：公开包免费） |
| Static Web Apps | Free 计划，100 GB/月带宽 | **$0** |
| Log Analytics | 日志量很小 | **约 $0** |
| 入站流量（从 NASA 下载） | 约 360 GB/月 | **$0**（入站免费） |
| 跨区域流量 | 同区域部署，见第 2.4 节 | **$0** |
| **合计** | | **$0/月** —— 全部落在免费额度内，$100 学生额度不动 |

> **唯一的前提：ghcr.io 上的镜像必须是 public。** private 镜像会开始计存储和流量费，而且 Container Apps 也拉不到。第 4.4 / 4.5 节就是确认这件事。

**Container Apps 免费额度**（每订阅每月，永久有效，不是学生专属）：

- 180,000 vCPU-秒
- 360,000 GiB-秒

**实测单次耗时约 3-7 分钟，取决于要向前回退跳过多少坏文件：**

| 情况 | 单次耗时 | 月度 vCPU-秒（360 次）| 占 180,000 额度 |
|---|---|---|---|
| 第一个候选就健康 | ~3 分钟 | 129,600 | 72% —— **$0** |
| 需要回退跳过 9 个坏文件（2026-09-16 的实测情况）| ~7 分钟 | 302,400 | **168% —— 超出** |

超出的部分按量计费，最坏情况约 **$3.7/月**（vCPU-秒 ~$2.94 + GiB-秒 ~$0.73），从 $100 额度里扣。

> **结论：NASA 正常时是 $0；NASA 连续出坏文件的那几天会花几美元。** 这不是 bug，是坏数据逼着管线多读了文件。
>
> 想压回去有两个办法：
>
> 1. **把 cron 从每 2 小时改成每 4 小时**（`--cron-expression "48 */4 * * *"`），用量直接减半
> 2. **用 HEAD 的 Content-Length 预筛**：日志里每个候选的大小都是现成的，而实测 400 MB 以下的文件必然是坏的（09-15 16:00Z = 416 MB/49 死块，09-16 03:00Z = 392 MB/48 死块）。加一条"小于阈值直接跳过"的判断，能省掉每次约 15-18 秒的无效读取。这个改动需要动 `gcc_load.py`，要做的话告诉我

### 设个预算告警（强烈建议）

门户 → 搜索 **Cost Management（成本管理）** → **Budgets（预算）** → **Add**

- Scope：选你的订阅
- Amount：**10**（美元）
- Alert condition：**Actual** 达到 **50%** 时发邮件
- 再加一条：Actual 达到 **90%**

这样万一有意外消费，你会收到邮件，而不是月底才发现。

### Log Analytics 是唯一容易出意外的地方

容器日志会进 Log Analytics 工作区。给工作区设个每日上限：

门户 → 搜 **Log Analytics workspaces** → 选自动创建的那个（名字类似 `<ENV>-logs`）→ **Usage and estimated costs** → **Daily Cap** → 设 **0.5 GB**。

---

## 13. 改 App 的地址

管线跑起来后，需要把 Android App 指向新地址。

编辑 `rewrite/app/src/main/java/com/example/marvelousmarble/utils/DataUrls.java`：

```java
private static final String GITHUB_USER = "unknown70022024";
private static final String REPO_NAME   = "polar_plus";

/** GitHub Pages base URL */
public static final String BASE_URL = "https://" + GITHUB_USER + ".github.io/" + REPO_NAME;
```

改成：

```java
/** Azure Static Web Apps base URL */
public static final String BASE_URL = "https://<你的>.azurestaticapps.net";
```

然后重新构建 APK、安装。

> **已装的旧版本怎么办**：旧版的 `LATEST_ROOT` 指向 GitHub Pages。如果你还想让老客户端能用，可以继续往 GitHub Pages 发一个 `root.json`，里面的 `baseUrl` 指向 Azure —— 老客户端读到 `baseUrl` 后会自动跟着去 Azure 拉瓦片，**不用更新 App**。
>
> 你能接受老版本停更的话，直接改代码重新发版即可。

---

## 14. 全部删掉

**删资源组 = 删掉里面所有东西**，停止计费：

```bash
az group delete --name "$RG" --yes --no-wait
```

`--no-wait` 表示不等完成就返回。实际删除要几分钟。

> **注意**：如果 SWA 是你从门户建的且放在了别的资源组，要单独删。

### 只删定时任务（保留镜像和站点）

```bash
az containerapp job delete -n "$JOB" -g "$RG" --yes
```

---

## 附：命令速查表

```bash
# 手动跑一次
az containerapp job start -n "$JOB" -g "$RG"

# 看执行历史
az containerapp job execution list -n "$JOB" -g "$RG" -o table

# 看日志
az containerapp job logs show -n "$JOB" -g "$RG" --container polar-plus --tail 100 --format text

# 实时跟日志
az containerapp job logs show -n "$JOB" -g "$RG" --container polar-plus --follow --format text

# 看线上清单
curl -s "https://$SWA_HOST/root.json"

# 重新构建并部署
podman build -f azure/Dockerfile -t "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG" \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
podman push "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"
az containerapp job update -n "$JOB" -g "$RG" --image "ghcr.io/$GHCR_USER/polar-plus:$IMAGE_TAG"

# 全部删除
az group delete --name "$RG" --yes --no-wait
```
