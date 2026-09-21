# GitOps（Argo CD）

把「CI 拿集群凭据直接改集群」换成「**CI 只改 Git，Argo CD 负责让集群对齐 Git**」。

## 为什么这是升级，不只是换个工具

| | 旧流程 | GitOps（现在） |
|---|---|---|
| 谁改集群 | CI 拿着 ssh 私钥 `kubectl apply` | Argo CD 从 Git 拉取并对齐 |
| CI 的权限 | **需要能改生产集群** | 只需要写仓库的权限 |
| 集群实际状态与期望状态的差异 | 不可知 | 随时可查（`Synced` / `OutOfSync`）|
| 有人手动 `kubectl edit` 了 | 没人知道，下次部署才覆盖 | `selfHeal` 自动纠回，且能查到 |
| 回滚 | 重跑一次流水线（构建新的旧版本镜像）| `git revert` —— 一次提交的事 |
| 审计 | 看流水线日志 | 看 Git 历史（谁、什么时候、改了什么）|

**核心变化**：集群里跑的东西不再取决于「CI 那一刻做了什么」，而是取决于
「仓库里现在写着什么」。这是可验证、可回滚、可审计的。

## 完整闭环

```
开发者 git push
      ↓
GitHub Actions：单元测试 → 构建多架构镜像 → 推 GHCR
      ↓
CI 把新镜像 tag 写进 k8s/kustomization.yaml，commit & push
  （提交信息带跳过标记，否则会再触发一轮流水线形成无限循环）
      ↓
Argo CD 每 60 秒轮询一次 Git，发现变化 → 自动同步
      ↓
集群滚动更新 → CI 轮询确认镜像已变成新 tag → 冒烟测试
```

**注意最后一步**：流水线里保留了 SSH，但只做**只读验证**
（等同步、等滚动更新、curl healthz）。
「CI 有读权限做验收」和「CI 有写权限直接改集群」是两件事 —— 前者有价值，后者是风险。

## 文件

| 文件 | 作用 |
|---|---|
| `values/argocd.yaml` | Argo CD 的 Helm values（资源限制、子路径、reconciliation 间隔）|
| `manifests/application.yaml` | Application 声明：从哪个仓库、哪个路径、同步到哪、用什么策略 |
| `manifests/argocd-ingress.yaml` | Argo CD UI 的 catch-all Ingress |

## 安装

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update argo

helm install argocd argo/argo-cd -n argocd --create-namespace \
  --version 10.9.2 -f gitops/values/argocd.yaml

kubectl apply -f gitops/manifests/argocd-ingress.yaml
kubectl apply -f gitops/manifests/application.yaml
```

访问：`https://<隧道域名>/argocd`
用户名 `admin`，密码：

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo
```

> 首次登录后建议改密码并删掉这个 secret。

## 日常操作

```bash
# 看同步状态和健康状态
kubectl -n argocd get application radar
kubectl -n argocd get application radar -o jsonpath='{.status.sync.status} {.status.health.status}'; echo

# 看哪些资源不同步（drift）
kubectl -n argocd get application radar -o jsonpath='{range .status.resources[*]}{.kind}/{.name}: {.status}{"\n"}{end}'

# 手动触发一次同步（不等 60 秒轮询）
# 装 argocd CLI 后：argocd app sync radar

# 看某次同步的操作历史
kubectl -n argocd get application radar -o jsonpath='{.status.history[*].revision}'; echo
```

## 演示「自愈」：手动改集群会被自动纠回

这是 GitOps 最直观的价值。手动把副本数改掉：

```bash
kubectl -n radar scale deployment/radar --replicas=3
kubectl -n radar get deploy radar       # 看到 3
```

等一轮 reconciliation（最长 60 秒），再看：

```bash
kubectl -n radar get deploy radar       # 回到 1 —— Argo CD 把 drift 纠回来了
kubectl -n argocd get application radar -o jsonpath='{.status.sync.status}'   # Synced
```

**这同时也意味着**：「临时手动改一下应急」在 GitOps 下是行不通的 ——
你必须改 Git。这是设计取舍，不是缺陷。

## 回滚

```bash
# 方式一：git revert（推荐，留痕）
git revert <坏提交>
git push
# Argo CD 自动同步回旧状态

# 方式二：在 Argo CD UI 里对某次历史版本点 Rollback（临时，会被下一次同步覆盖）
```

## 踩过的坑

### 1. 提交信息里写出跳过标记的字面量，会让这次提交自己被跳过

CI 回写 tag 时必须让那次提交不触发流水线，否则：回写 → 触发流水线 → 又回写 → 无限循环。
GitHub 的做法是在**提交信息里包含特定标记**。

但那个标记是**按提交信息全文匹配**的。我写提交信息解释「为什么要加这个标记」的时候，
把标记原文写了进去 —— 结果那次提交自己也被跳过了，流水线**完全没跑**，
而且不报任何错（Actions 页面里连一个失败的运行都没有）。

**教训**：解释这个机制的时候要改写成描述性说法，别写出字面量。

### 2. Argo CD chart 的 ingress 对空 hosts 有 fallback

想要「catch-all 规则」（因为隧道域名是随机的），需要不带 host 的 Ingress。
但 chart 的 `server.ingress` 模板在 `hosts: []` 时**仍然会渲染 `host: argocd.example.com`**，
导致真实域名访问 404。

**解法**：`server.ingress.enabled=false`，自己写 Ingress（见 `argocd-ingress.yaml`）。
教训同上：**别信"设成空就会没有"**，一定要 `kubectl get ingress` 回读确认。

### 3. 子路径部署要同时设 basehref 和 rootpath

Argo CD 挂 `/argocd` 子路径时，只设 `server.rootpath` 会出现静态资源 404。
必须：

```yaml
configs:
  params:
    server.basehref: /argocd
    server.rootpath: /argocd
    server.insecure: "true"     # 后端走明文 HTTP，ingress 就不用处理 TLS 回源
```

### 4. 同步有延迟，流水线要等

Argo CD 默认 3 分钟才轮询一次 Git。本项目的 CI 会等「镜像变成新 tag」，
所以把 `timeout.reconciliation` 调成了 60 秒，避免流水线白等。

**这是 GitOps 的固有代价**：多了一层「提交 → 轮询发现 → 同步」的延迟，
换来的是可审计和可回滚。急着上线可以在 CI 里主动调 Argo CD 的 sync API，
但那就又把「谁能改集群」的权限还给了 CI，取舍自行判断。

## 和监控的配合

Argo CD 自己的指标没接进 Prometheus（关掉了 notifications，也没开 metrics serviceMonitor），
因为对单节点实验环境来说，**集群同步状态用 `kubectl get application` 看就够了**。
如果要上生产，值得加：

- `argocd_app_info{sync_status!="Synced"}` → 有应用不同步时告警
- `argocd_app_info{health_status!="Healthy"}` → 应用不健康时告警
