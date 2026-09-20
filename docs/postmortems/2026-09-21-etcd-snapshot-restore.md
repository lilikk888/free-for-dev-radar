# 演练报告：etcd 快照备份与真实恢复

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-21 01:34 (GMT+8) |
| 环境 | OCI 东京 ARM 单节点集群 `free-arm-2c12g`，kubeadm v1.36.4，etcd 3.6.8 |
| 类型 | 计划内演练（计划性变更） |
| 结果 | ✅ 成功，数据完整回滚到快照时刻 |
| 实测不可用时长 | **约 90 秒** |
| 触发方式 | 手动执行，无用户侧影响（个人实验环境） |

## 1. 演练目标

回答一个面试高频问题：**etcd 数据目录被人删了 / 损坏了，你怎么恢复？**

不是「我知道要备份」，而是真的做一遍，并留下可复现的步骤和证据。

## 2. 前置侦察（这一步不能省）

```bash
# 看证书还有多久过期（顺带确认 kubeadm 证书体系健康）
sudo kubeadm certs check-expiration

# 找 etcd 数据目录、名称、peer 地址 —— 恢复时要逐个对齐
sudo grep -E "data-dir|name=|initial-cluster|initial-advertise-peer|listen-client" \
  /etc/kubernetes/manifests/etcd.yaml

# etcd 版本
kubectl -n kube-system get pod etcd-free-arm-2c12g -o jsonpath="{.spec.containers[0].image}"
```

侦察结论（恢复命令要用的参数）：

| 参数 | 值 |
|---|---|
| data-dir | `/var/lib/etcd` |
| --name | `free-arm-2c12g` |
| --initial-cluster | `free-arm-2c12g=https://10.0.2.134:2380` |
| --initial-advertise-peer-urls | `https://10.0.2.134:2380` |
| etcd 版本 | 3.6.8（镜像 `registry.k8s.io/etcd:3.6.8-0`） |

### ⚠️ 第一个坑：kubeadm 不装 etcdctl

主机上**没有** `etcdctl`。要自己装，且**版本必须和 etcd 对齐**：

```bash
curl -fsSL -o /tmp/etcd.tgz \
  "https://github.com/etcd-io/etcd/releases/download/v3.6.8/etcd-v3.6.8-linux-arm64.tar.gz"
tar xzf /tmp/etcd.tgz -C /tmp
sudo install -m 755 /tmp/etcd-v3.6.8-linux-arm64/etcdctl /usr/local/bin/etcdctl
sudo install -m 755 /tmp/etcd-v3.6.8-linux-arm64/etcdutl /usr/local/bin/etcdutl
```

### ⚠️ 第二个坑：etcd 3.5+ 把「离线操作」拆到了 `etcdutl`

`etcdctl` 只剩**需要连服务端**的操作（`snapshot save`）；
**离线读快照和恢复都挪到了 `etcdutl`**：

| 操作 | etcd 3.4 及以前 | etcd 3.5+ |
|---|---|---|
| 备份（连服务端） | `etcdctl snapshot save` | `etcdctl snapshot save`（不变） |
| 查看快照内容 | `etcdctl snapshot status` | **`etcdutl snapshot status`** |
| 从快照恢复 | `etcdctl snapshot restore` | **`etcdutl snapshot restore`** |

演练时就踩到了：`etcdctl snapshot status` 直接打印 help（子命令不存在），
换成 `etcdutl` 才成功。

## 3. 操作步骤

### 3.1 备份并**验证可读**

```bash
sudo mkdir -p /var/backups/etcd
sudo etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /var/backups/etcd/snapshot-before-drill.db

# 关键：能读出元数据才算真备份。只生成文件不代表它能恢复。
sudo etcdutl snapshot status /var/backups/etcd/snapshot-before-drill.db --write-out=table
```

实测输出：

```
+----------+----------+------------+------------+---------+
|   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE | VERSION |
+----------+----------+------------+------------+---------+
| 8bccf254 |   185833 |        945 |      31 MB |   3.6.0 |
+----------+----------+------------+------------+---------+
```

### 3.2 制造「快照之后才存在」的资源

**这是整个演练最关键的设计** —— 光看集群起来了不能证明恢复生效，可能是原数据还在。
必须制造一个**只在快照之后存在**的差异点，恢复后它必须消失：

```bash
kubectl -n default create configmap drill-marker --from-literal=created=after-snapshot
# 创建时间 2026-09-20T17:34:05Z（快照拍于 17:33:32，晚于快照）
```

### 3.3 停控制面

```bash
sudo mkdir -p /etc/kubernetes/manifests-stopped
for f in etcd kube-apiserver kube-controller-manager kube-scheduler; do
  sudo mv /etc/kubernetes/manifests/$f.yaml /etc/kubernetes/manifests-stopped/
done
```

kubelet 监控 `/etc/kubernetes/manifests/`，文件一移走它就会停掉对应静态 Pod。
验证 apiserver 确实停了：`sudo ss -lnt | grep :6443` 无输出，`kubectl` 报 `connection refused`。

### 3.4 数据目录挪走（**绝不直接删**）

```bash
sudo mv /var/lib/etcd /var/lib/etcd.corrupted-$(date +%Y%m%d-%H%M%S)
```

原目录 274M，保留下来是为了万一恢复失败还能回退、也便于事后分析。

### 3.5 从快照恢复

```bash
sudo etcdutl snapshot restore /var/backups/etcd/snapshot-before-drill.db \
  --name=free-arm-2c12g \
  --initial-cluster=free-arm-2c12g=https://10.0.2.134:2380 \
  --initial-advertise-peer-urls=https://10.0.2.134:2380 \
  --data-dir=/var/lib/etcd
```

`--name` / `--initial-cluster` / `--initial-advertise-peer-urls` 必须和原 manifest 对齐，
否则恢复出来的成员身份对不上，etcd 起不来。

### 3.6 恢复控制面

```bash
sudo mv /etc/kubernetes/manifests-stopped/*.yaml /etc/kubernetes/manifests/
```

静态 Pod 由 kubelet 自动重建，约 60 秒后节点 Ready。

## 4. 恢复验证（证据）

| 检查项 | 结果 |
|---|---|
| **`drill-marker` ConfigMap** | ✅ **已消失** —— 数据确实回滚到快照时刻 |
| `etcdctl endpoint health` | ✅ healthy |
| 控制面 4 个静态 Pod | ✅ 全部 1/1 Running |
| 节点状态 | ✅ `Ready`，v1.36.4 |
| 业务负载（radar / Grafana / Loki / Prometheus） | ✅ **全部 Running，一个都没丢** |
| 应用与 Grafana 通过 Ingress | ✅ 200 / 200 |
| 损坏数据目录 | ✅ 保留未删 |

### 停机期间的连锁反应（从 kube-system 事件里读到）

控制面一停，依赖 apiserver 的组件立刻开始报错：

```
Warning  Unhealthy  pod/calico-kube-controllers  Liveness probe failed:
  Error verifying datastore: Get "https://10.96.0.1:443/apis/crd.projectcalico.org/v1/
  clusterinformations/default": dial tcp 10.96.0.1:443: connect: connection refused
```

apiserver 回来后自动选主，无需人工介入：

```
Normal   LeaderElection  lease/kube-scheduler
  free-arm-2c12g_59de1895-... became leader
Normal   LeaderElection  lease/kube-controller-manager
  free-arm-2c12g_b89eba1e-... became leader
```

> 注意：**静态 Pod 的容器在停控制面期间并没有被删**，所以业务 Pod 一直在跑
> （kubelet 继续维持已有容器）。控制面不可用影响的是「新的调度和状态变更」，
> 不是「已运行的负载」——这是理解 K8s 控制面与数据面分离的一个具体例子。

## 5. 演练中暴露的真实事故：CI 部署失败

**这是个意外收获，而且是我自己的操作失误造成的。**

| 项目 | 内容 |
|---|---|
| 现象 | GitHub Actions 的「部署到 Kubernetes」步骤失败 |
| 时间 | 2026-09-20 17:34:37 UTC |
| 报错 | `dial tcp 10.0.2.134:6443: connect: connection refused` |

**根因**：我在 **17:34:36** 停掉控制面做演练，而流水线正好在 **17:34:37**
执行 `kubectl apply` —— 演练窗口和发布窗口重叠了。相差 1 秒。

**这暴露的是流程问题，不是技术问题**：

- 单节点集群上做控制面维护 = 整个集群不可用
- **变更（含演练）必须和 CI/CD 的自动发布错开**
- 如果这是多人团队，正确做法是：宣布维护窗口 → 暂停 CD（例如临时把工作流置为 disabled）
  → 或在部署前置检查里探测 apiserver 可达性并给出明确提示

**处置**：控制面恢复后 `gh run rerun --failed` 重跑部署，成功。

**改进项**（见下一节 AI-3）。

## 6. Action Items

| 编号 | 改进项 | 优先级 | 状态 |
|---|---|---|---|
| AI-1 | 把 etcd 备份脚本化 + 加定时任务（当前只是一次性手动备份） | 高 | 待办 |
| AI-2 | 备份文件异机留存（现在和 etcd 同盘，机器挂了备份一起没） | 高 | 待办 |
| AI-3 | 给 CI 的部署步骤加**前置可达性检查**，失败时输出人话而不是一堆 `connection refused` | 中 | 待办 |
| AI-4 | 建一份「控制面维护窗口」checklist：先确认无流水线在跑、再动手 | 中 | 待办 |
| AI-5 | 加一条 `kube_pod_container_status_restarts_total` 突增的告警，覆盖静态 Pod 意外重启 | 低 | 待办 |
| AI-6 | 演练等等间隔重复（建议每季度），并把耗时记录进本文件 | 低 | 待办 |

## 7. 可复用 SOP（浓缩版）

```bash
# ===== 备份 =====
sudo etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /var/backups/etcd/snapshot-$(date +%Y%m%d-%H%M%S).db
sudo etcdutl snapshot status /var/backups/etcd/snapshot-*.db --write-out=table   # 必须能读出元数据

# ===== 恢复 =====
sudo mkdir -p /etc/kubernetes/manifests-stopped
for f in etcd kube-apiserver kube-controller-manager kube-scheduler; do
  sudo mv /etc/kubernetes/manifests/$f.yaml /etc/kubernetes/manifests-stopped/; done
sudo mv /var/lib/etcd /var/lib/etcd.corrupted-$(date +%Y%m%d-%H%M%S)   # 不删！
sudo etcdutl snapshot restore <快照文件> \
  --name=<节点名> --initial-cluster=<节点名>=https://<节点IP>:2380 \
  --initial-advertise-peer-urls=https://<节点IP>:2380 --data-dir=/var/lib/etcd
sudo mv /etc/kubernetes/manifests-stopped/*.yaml /etc/kubernetes/manifests/
sleep 60 && kubectl get nodes
```

**三条铁律**：
1. 备份后**必须验证可读**，只生成文件不等于能恢复
2. 数据目录**只挪不删**
3. 恢复参数（`--name` / `--initial-cluster` / peer URL）**逐字对齐**原 manifest
