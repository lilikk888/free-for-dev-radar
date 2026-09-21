# 集群运维手册

记录这个集群**不写在代码里、但不知道就会踩坑**的配置。换台机器重建时要照着做。

## 集群基本信息

| 项 | 值 |
|---|---|
| 安装方式 | kubeadm v1.36.4（单节点起步） |
| 控制面节点 | `free-arm-2c12g`（Oracle Cloud 东京，ARM **aarch64**，2 OCPU / 12 GB） |
| 控制面私网 IP | `10.0.2.134` |
| 公网访问 | Cloudflare Tunnel（**不需要开任何入站端口到公网**） |
| Pod 网段 | `192.168.0.0/16` |
| CNI | Calico v3.30.0 |
| 容器运行时 | containerd 2.3.4（`SystemdCgroup = true`） |
| 存储 | local-path-provisioner |
| 入口 | ingress-nginx（DaemonSet + hostPort 80/443） |

## OCI 网络配置（重建时最容易漏）

### 三台实例都在同一个 VCN、同一个子网

| 实例 | 私网 IP | 公网 IP | 架构 | 角色 |
|---|---|---|---|---|
| free-arm-2c12g | 10.0.2.134 | 193.123.164.23 | aarch64 | K8s 控制面 |
| instance-20260902-1214 | 10.0.2.22 | 161.33.148.174 | x86_64 | 备用 |
| instance-20260902-1251 | 10.0.2.63 | 161.33.149.235 | x86_64 | etcd 异机备份 |

子网 CIDR `10.0.2.0/24`，网关 `10.0.2.1`，VCN CIDR `10.0.0.0/16`。

> ⚠️ **同一个子网 = 共用同一份安全列表**。也就是加一条规则三台一起生效
> （但安全列表只是「允许」，不是「监听」—— 没进程监听端口的机器不会因此暴露什么）。
> 想按实例分别控制要用 **NSG**（网络安全组），它挂在实例网卡上。

### 需要放行的端口（安全列表 / NSG，源限制 `10.0.0.0/16`）

| 源 | 协议 | 端口 | 用途 |
|---|---|---|---|
| `10.0.0.0/16` | TCP | `6443` | apiserver（kubeadm join 必需）|
| `10.0.0.0/16` | TCP | `10250` | kubelet（`kubectl logs/exec`、探针必需）|
| `10.0.0.0/16` | TCP | `2379-2380` | etcd（做多控制面才需要）|
| `10.0.0.0/16` | 协议号 `4` | — | IPIP —— **仅在节点跨子网时才需要**，见下 |

**默认的 22 / 80 / 443 是给别的东西用的**，和 K8s 无关。
apiserver 只在内网监听 + 隧道出站，**公网不需要放行 6443**。

> ⚠️ 实测踩到：安全列表只放行 22/80/443 时，节点能 SSH 到控制面，
> 但 `kubeadm join` 会因为 `10.0.2.134:6443` 不通而失败。
> 症状很像"网络没问题"，实际是安全列表缺规则。

## Calico 用 CrossSubnet 而不是默认的 Always

```bash
kubectl get ippool default-ipv4-ippool -o jsonpath='{.spec.ipipMode}'   # CrossSubnet
```

| 模式 | 同子网节点间 | 跨子网节点间 |
|---|---|---|
| `Always`（Calico 默认）| **IPIP 封装** | IPIP 封装 |
| **`CrossSubnet`（本集群）** | **直接路由** | IPIP 封装 |
| `Never` | 直接路由 | 直接路由（跨子网会不通）|

**为什么改**：本集群所有节点都在 `10.0.2.0/24`，同子网之间走直接路由即可
（数据帧按 MAC 送达，Pod 网段只在 IP 头里，不需要 ARP 到 Pod IP）。
这样就**不需要在安全列表里放行 IP 协议号 4** —— 而很多云控制台的协议下拉里
根本没有"自定义协议号"这一项，加不了。

改法（可在线改，改完滚动重启 calico-node）：

```bash
kubectl patch ippool default-ipv4-ippool --type merge \
  -p '{"spec":{"ipipMode":"CrossSubnet","vxlanMode":"Never"}}'
kubectl -n kube-system rollout restart daemonset/calico-node
```

> ⚠️ 如果以后把节点加到了**别的子网**，就必须回头放行 IP 协议号 4（IPIP），
> 或者改用 VXLAN（`vxlanMode: CrossSubnet` + 放行 UDP 4789 —— 这个端口好加得多）。

## ⚠️ 加节点前必读：两个坑 + 一个硬结论

### 坑一：**两台机器上都有本地 iptables 规则在拦 k8s 端口**

这些实例的镜像/cloud-init 预置了一条"除 22/80/443 全拒绝"的规则：

```
-P INPUT ACCEPT
-A INPUT -p tcp --dport 22 -j ACCEPT
-A INPUT -p tcp --dport 80 -j ACCEPT      # 只有部分机器有
-A INPUT -p tcp --dport 443 -j ACCEPT
-A INPUT -j REJECT --reject-with icmp-host-prohibited   # ← 把上面没列的端口全挡了
```

**关键认知**：`-P INPUT ACCEPT`（默认策略是放行）看着很安全，但**这条 REJECT 排在前面**，
等于把默认策略架空了。所以**只看默认策略会误判**。

而且**必须两台都改**，方向是双向的：

| 方向 | 用途 | 改哪台 |
|---|---|---|
| 新节点 → 控制面:6443 | kubeadm join、kubelet 上报 | 控制面的 INPUT |
| 控制面 → 新节点:10250 | `kubectl logs/exec`、探针 | 新节点的 INPUT |
| 双向 TCP 179 | Calico BGP 交换路由 | 两台的 INPUT 都要 |

放行命令（只放 VCN 内网，不动那条 REJECT）：

```bash
for dp in 179 6443 10250 2379 2380; do
  sudo iptables -I INPUT -s 10.0.0.0/16 -p tcp --dport $dp -j ACCEPT
done
sudo netfilter-persistent save     # 持久化，否则重启就没了
```

> ⚠️ 顺带一个教训：我们一开始以为是 **OCI 安全列表**的问题，
> 让用户在控制台加规则，加完还是不通 —— 因为真正的拦截在**本机 iptables**。
> **排查网络不通时，先看本机 iptables，再看云平台防火墙**。

### 坑二：containerd 装完后必须 **restart**，不能只用 `enable --now`

```bash
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y containerd.io
sudo bash -c "containerd config default > /etc/containerd/config.toml"
sudo sed -i "s/SystemdCgroup = false/SystemdCgroup = true/" /etc/containerd/config.toml
sudo systemctl enable --now containerd    # ❌ 如果它已经在跑，这行是空操作
sudo systemctl restart containerd         # ✅ 必须显式 restart 才会读新配置
```

**症状**：`kubeadm join` 报
```
[ERROR CRI]: could not connect to the container runtime:
  unknown service runtime.v1.RuntimeService
```
这是 **CRI 插件没加载**的典型特征（配置没被读取，containerd 用的还是默认状态）。

### ⭐ 硬结论：**1 GB / 1-8 OCPU 的 OCI Micro 撑不住 k8s worker**

实测数据（Oracle E2.1.Micro，1 GB RAM + 1/8 OCPU，只有系统服务无额外负载）：

| 指标 | 实测值 | 说明 |
|---|---|---|
| join 本身 | ✅ 成功 | 节点能注册、能变 Ready |
| 稳定运行 | ❌ **24 分钟后仍 NotReady** | calico-node 永远起不来 |
| load average | **38 ~ 42** | 正常应 < 2 |
| 内存可用 | 137 MB | kubelet+containerd+calico+kube-proxy ≈ 340 MB |
| **磁盘 IO 等待** | **56% ~ 69%** | 真正的瓶颈不是 CPU |
| 持续块读 | **51 MB/s，24 分钟共 70+ GB** | 容器镜像解包在低 IOPS 引导卷上打转 |
| `crictl images` | 超时无响应 | containerd 完全卡死 |
| SSH | 握手超时 | 机器还在，但慢到无法交互 |

**根因**：容器镜像解包是 **IOPS 密集型**操作，而 Micro 规格的引导卷 IOPS/吞吐极低
（几百 IOPS 级别）。CPU 只有 1/8 OCPU 更是雪上加霜。**不是配置问题，是规格问题。**

**官方最低要求是 2 CPU / 2 GB** —— 实测确认这个数字不是随便写的。

**后果提醒**：那台 Micro 上还跑着 `x-ui`（用户的代理面板），
被 k8s 组件饿死后代理也不通了。**在资源紧张的机器上做实验前，
先确认上面有没有在跑别的服务。**

**真要第二个节点，应该用**：本地虚拟机（内存够、磁盘快，可走 WireGuard 接入）、
或一台 ≥2C/2G 的云主机。跨云接入还要额外处理 apiserver 暴露与网络封装，不划算。

## 控制面指标开放

kubeadm 默认把 scheduler / controller-manager / etcd 的 metrics 只绑 `127.0.0.1`，
Prometheus 抓不到。用脚本打开：

```bash
sudo NODE_IP=10.0.2.134 bash observability/scripts/enable-control-plane-metrics.sh
```

详见 [`observability/README.md`](../observability/README.md) 的「坑一」。

## 日常运维命令

```bash
# --- 集群状态 ---
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running      # 只看异常
kubectl get application -n argocd          # GitOps 同步状态

# --- 控制面 ---
sudo kubeadm certs check-expiration        # 证书还有多久过期
sudo systemctl status kubelet containerd
sudo crictl ps                             # 容器运行时视角

# --- 排查节点不通 ---
# 从另一台机器测端口，而不是只看 SSH 通不通
for p in 22 6443 10250; do
  printf "%s -> " "$p"; timeout 3 bash -c "</dev/tcp/10.0.2.134/$p" && echo 通 || echo 不通
done

# --- 恢复 ---
# etcd 备份与恢复：见 ops/etcd-backup/README.md 和
#                 docs/postmortems/2026-09-21-etcd-snapshot-restore.md
# ⚠️ 恢复后必须重启依赖 informer 缓存的组件（kube-state-metrics、各 operator）
```

## 相关文档

| 主题 | 位置 |
|---|---|
| 可观测性（Prometheus/Grafana/Loki） | [`observability/README.md`](../observability/README.md) |
| etcd 备份与恢复 | [`ops/etcd-backup/README.md`](../ops/etcd-backup/README.md) |
| GitOps / Argo CD | [`gitops/README.md`](../gitops/README.md) |
| 演练与事故报告 | [`docs/postmortems/`](../docs/postmortems/README.md) |
