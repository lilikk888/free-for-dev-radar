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
