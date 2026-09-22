# 集群运维手册

记录这个集群**不写在代码里、但不知道就会踩坑**的配置。换台机器重建时要照着做。

## 一、集群基本信息（截至 2026-09-22）

| 项 | 值 |
|---|---|
| 安装方式 | kubeadm **v1.36.4**，三节点跨云 |
| Pod 网段 | `192.168.0.0/16` |
| Service 网段 | `10.96.0.0/12` |
| CNI | Calico v3.30.0，**VXLAN 模式**（`ipipMode: Never` / `vxlanMode: Always`） |
| 容器运行时 | containerd（各节点 2.2.x~2.3.x，均 `SystemdCgroup = true`） |
| 全局 MTU | **1280**（`calico-config` 的 `veth_mtu`，原因见「跨云」一节） |
| 存储 | local-path-provisioner |
| 入口 | ingress-nginx（DaemonSet + hostPort 80/443，已用 nodeSelector 限定在主控） |
| 公网访问 | Cloudflare Tunnel（**不需要向公网放开任何端口**） |

### 三个节点

| 节点名 | 云 | 地区 | 架构 | 角色 | 地址 | 规格 |
|---|---|---|---|---|---|---|
| `free-arm-2c12g` | Oracle | 东京 | arm64 | 控制面 | `10.0.2.134` | 2 OCPU / 12 GB |
| `instance-20260902-1251` | Oracle | 东京 | amd64 | worker | `10.0.2.63` | 1/8 OCPU / 1 GB |
| `iz7xvdabk2g1oasakx27fnz` | 阿里云 | 广州 | amd64 | worker | `10.9.0.2`（隧道） | 2 vCPU / 1.6 GB |

节点标签：`cloud=oracle|aliyun`、`topology.kubernetes.io/region=ap-tokyo-1|cn-guangzhou`

> ⭐ **这个集群本身就是个知识点**：三个节点跨两家云、跨两个地域、跨两种 CPU 架构（arm64 + amd64），
> 用 WireGuard 组成一张扁平网络，Calico 在上面跑 VXLAN overlay。
> 跨架构能成立的前提是**镜像必须多架构**（radar 自己的镜像是 amd64+arm64 双架构）。

## 二、网络拓扑与隧道

```
Oracle 东京 10.0.2.0/24 ─┐
  ├ 10.0.2.134 控制面 ◄──┼── WireGuard (UDP 51820) ── 阿里云广州 10.9.0.2
  └ 10.0.2.63  worker    │       10.9.0.0/30            隧道内网
                          └─ 同子网，走 VPC 直接通信
```

- 隧道网段 `10.9.0.0/30`：主控 `10.9.0.1`、阿里云 `10.9.0.2`，**MTU 1380**
- 阿里云节点用 `--node-ip=10.9.0.2` 注册，所以它的 kubelet/Calico 都走隧道
- 阿里云侧 `AllowedIPs = 10.9.0.0/30, 10.0.2.0/24`（要能直连 `10.0.2.134:6443`）
- 主控侧 `AllowedIPs = 10.9.0.2/32`
- Micro 加了静态路由 `10.9.0.0/30 via 10.0.2.134`（systemd 单元 `route-wg.service`），
  否则 Micro 上的 Pod 找不到阿里云节点

**为什么不让阿里云节点直接连 apiserver 的隧道 IP？** 因为 apiserver 证书 SAN 里没有 `10.9.0.1`，
而**有** `10.0.2.134`。让隧道把 `10.0.2.0/24` 也路由过来，就能复用现成证书，**不用动证书**。

### 跨云带来的三个必然调整

| 问题 | 现象 | 处理 |
|---|---|---|
| VXLAN 装不下 | 默认 VXLAN MTU 1450，进不了 MTU 1380 的隧道 | 全局 `veth_mtu: 1280` |
| Calico 选错网卡 | 阿里云节点会选到 `eth0`（172.29.x，对端不可达） | `IP_AUTODETECTION_METHOD=can-reach=10.0.2.134` |
| 延迟高 | 东京↔广州实测 **165 ms** | 给阿里云节点打 `PreferNoSchedule` 污点，重要负载不调度过去 |

## 三、需要放行的端口

### OCI 安全列表（源 `10.0.0.0/16`）

| 协议 | 端口 | 用途 |
|---|---|---|
| TCP | `6443` | apiserver |
| TCP | `10250` | kubelet（`logs/exec`、探针） |
| TCP | `9100` | node-exporter（Prometheus 抓主机指标） |
| TCP | `10249` | kube-proxy 指标 |
| **UDP** | **`4789`** | **VXLAN 跨节点 Pod 网络（必需）** |
| TCP | `2379-2380` | etcd（只有加第二个控制面才需要） |

> ⚠️ OCI 控制台里 UDP 端口要选「**自定义 UDP**」才能填端口；选「所有 UDP」会锁死成全部端口。

### 阿里云安全组（入方向，源 `193.123.164.23/32`）

| 协议 | 端口 | 用途 |
|---|---|---|
| UDP | `51820` | WireGuard 隧道（**唯一需要开的口**） |

## 四、⚠️ 加节点前必读：四个坑

### 坑一：**本机 iptables 在拦，而且 INPUT 和 FORWARD 各埋了一条**

Oracle 实例镜像预置：

```
-P INPUT ACCEPT          ← 默认策略是「放行」，看着像没防火墙
-A INPUT  -p tcp --dport 22 -j ACCEPT
-A INPUT  -j REJECT --reject-with icmp-host-prohibited   ← 把默认策略架空了
-A FORWARD -j REJECT --reject-with icmp-host-prohibited  ← 这条更隐蔽！
```

- **INPUT 上的 REJECT** 会拦节点间通信（10250、4789、179…）
- **FORWARD 上的 REJECT** 会拦 **Pod 的跨节点流量**（Pod 的包要经 FORWARD 转发出去）

⭐ **两条都要处理，而且必须插在 REJECT 之前**（不是清空防火墙）：

```bash
# INPUT：给 VCN 内网和隧道网段放行
sudo iptables -I INPUT <REJECT的行号> -s 10.0.0.0/16  -j ACCEPT
sudo iptables -I INPUT <REJECT的行号> -s 10.9.0.0/30  -j ACCEPT   # 跨云隧道
# FORWARD：Pod 转发放行
sudo iptables -I FORWARD <REJECT的行号> -s 192.168.0.0/16 -j ACCEPT
sudo iptables -I FORWARD <REJECT的行号> -d 192.168.0.0/16 -j ACCEPT
sudo netfilter-persistent save
```

**行号怎么取**：`sudo iptables -L INPUT --line-numbers -n | awk '/REJECT/{print $1; exit}'`

> ⭐⭐ **最有价值的一条排查经验：错误码能区分拦截层**
>
> | 错误码 | 含义 | 去哪查 |
> |---|---|---|
> | `No route to host` | 有人 **REJECT**（回 ICMP host-prohibited） | **本机 iptables**（INPUT + FORWARD 都要看） |
> | `timeout` / 静默丢包 | **云平台防火墙**丢的 | OCI 安全列表 / 阿里云安全组 / NSG |
>
> 我们在这个集群上被同一类问题绊了 **四次**（Micro 的 INPUT、主控的 INPUT、
> Micro 的 FORWARD、WireGuard 的 ListenPort），前三次都误判成"云平台安全列表"。
> **结论：任何端口不通，先看 `iptables -L INPUT/FORWARD`，再看云控制台。**

### 坑二：containerd 必须 `restart`，`enable --now` 是空操作

已在运行的 containerd，`systemctl enable --now` 不会重读配置。

```bash
sudo bash -c "containerd config default > /etc/containerd/config.toml"
sudo sed -i "s/SystemdCgroup = false/SystemdCgroup = true/" /etc/containerd/config.toml
sudo systemctl restart containerd      # ✅ 必须 restart
```

**症状**：`kubeadm join` 报 `[ERROR CRI] unknown service runtime.v1.RuntimeService`。

> ⚠️ 另外注意 **docker 装的 containerd 默认 `disabled_plugins = ["cri"]`**，
> k8s 用不了，必须重新生成默认配置启用 CRI（阿里云那台就是这种情况，原配置已备份为
> `/etc/containerd/config.toml.bak-docker`）。

### 坑三：国内节点拉不到 `registry.k8s.io`

会 302 到 `europe-west3-docker.pkg.dev`（Google 域名），国内直接 i/o timeout。

用 containerd 2.x 的 `certs.d` 机制配镜像源：

```bash
mkdir -p /etc/containerd/certs.d/registry.k8s.io /etc/containerd/certs.d/docker.io

cat > /etc/containerd/certs.d/registry.k8s.io/hosts.toml << 'EOF'
server = "https://registry.k8s.io"
[host."https://k8s.m.daocloud.io"]
  capabilities = ["pull", "resolve"]
EOF

cat > /etc/containerd/certs.d/docker.io/hosts.toml << 'EOF'
server = "https://docker.io"
[host."https://docker.m.daocloud.io"]
  capabilities = ["pull", "resolve"]
EOF
```

然后把配置里 **CRI 那一段**的 `config_path` 指过去：

```bash
# ⚠️ 不要用 sed 全局替换！config.toml 里有三处同名 config_path，改错会让 containerd 起不来
# 只改 [plugins.'io.containerd.cri.v1.images'.registry] 段下那一个：
#     config_path = "/etc/containerd/certs.d"
sudo systemctl restart containerd
```

**验证方法（重要）**：必须用 `ctr images pull --hosts-dir /etc/containerd/certs.d <ref>`，
直接 `ctr images pull` **不走 CRI 的镜像配置**，会误判成"还是不通"。

> ⚠️ 阿里云官方的 `registry.aliyuncs.com/google_containers` 需要授权，实测拉不动；
> daocloud 的公共镜像源实测可用。

### 坑四：WireGuard 两端都要显式写 `ListenPort`

只在一端写端口，另一端会监听在**随机端口**，握手包到了却没人接。

**症状**：一端 `transfer: 0 B received`，另一端抓包能看到包已经送达。

```bash
sudo wg show        # 一眼看 listening port 对不对
```

## 五、1 GB Micro 能不能当 worker？—— 能，但要治两处

这台 `1/8 OCPU + 1 GB` 的 Micro 最初实测是「撑不住」的（load 42、IO 等待 69%），
但**治掉下面两处之后它可以稳定工作**：

### 1. 关掉系统自带的自动更新任务（真正吃 CPU 的元凶）

实测那台机器上 CPU 的大头是 **snapd / apt-check / check-new-release / unattended-upgrades**，
不是 k8s 组件。1/8 OCPU 的机器上它们一跑，磁盘和 CPU 立刻被打满：

```bash
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer
sudo systemctl mask apt-daily.timer apt-daily-upgrade.timer
sudo systemctl disable --now snapd.service snapd.socket snapd.seeded.service
sudo systemctl disable --now unattended-upgrades.service packagekit.service
sudo systemctl mask motd-news.timer
```

### 2. 摆正它的资源定位

`kubectl top` 实测（装好 metrics-server 之后）：

| 节点 | CPU | 内存 |
|---|---|---|
| 主控（2 OCPU / 12 GB） | 18% | 43% |
| **Micro（1/8 OCPU / 1 GB）** | **4%** | **94%** ← 瓶颈在内存 |
| 阿里云（2 vCPU / 1.6 GB） | 3% | 63% |

- CPU 平均只有 4%，但**突发时会被主机的 quota 卡住（steal 能到 60%+）**
- 内存是硬约束：**不要往这台调度业务 Pod**，让它只跑 DaemonSet（calico/kube-proxy/node-exporter/promtail）
- 配合 `PreferNoSchedule` 污点或 nodeSelector 把业务负载引到另外两台

> **结论更新**：早期结论「1 GB 完全不能用」过于绝对。准确说法是
> **「1 GB 能跑 worker，但只能承载 DaemonSet 级别的负载，且必须关掉系统自动更新」**。

## 六、控制面指标开放

kubeadm 默认把 scheduler / controller-manager / etcd 的 metrics 只绑 `127.0.0.1`：

```bash
sudo NODE_IP=10.0.2.134 bash observability/scripts/enable-control-plane-metrics.sh
```

详见 [`observability/README.md`](../observability/README.md) 的「坑一」。

## 七、网络策略与资源视图

- **NetworkPolicy**（Calico 策略引擎）：`k8s/networkpolicy.yaml`
  - `radar` 命名空间默认**拒绝所有入站**，只放行来自 `ingress-nginx`（外部访问）和
    `monitoring`（Prometheus 抓 `/metrics`）的 8000 端口
  - 出站不限制（采集任务要访问公网）
- **metrics-server**：已装（`kube-system`），提供 `kubectl top`，也是 **HPA** 的前提
- **HPA 演示**：`demos/hpa/`（独立命名空间，不影响业务）

## 八、日常运维命令

```bash
# --- 集群状态 ---
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running      # 只看异常
kubectl get application -n argocd          # GitOps 同步状态
kubectl top nodes / kubectl top pods -A    # 需要 metrics-server

# --- 跨云隧道 ---
sudo wg show                               # 握手、流量计数
ping -c 3 10.9.0.2                         # 隧道通不通

# --- 控制面 ---
sudo kubeadm certs check-expiration        # 证书还有多久过期
sudo systemctl status kubelet containerd

# --- 排查节点不通（先本机 iptables，再云防火墙）---
sudo iptables -L INPUT --line-numbers -n | tail -5
sudo iptables -L FORWARD --line-numbers -n | tail -5
for p in 6443 10250 4789; do
  printf "%s -> " "$p"; timeout 3 bash -c "</dev/tcp/10.0.2.134/$p" && echo 通 || echo 不通
done

# --- 恢复 ---
# etcd 备份与恢复：见 ops/etcd-backup/README.md
# ⚠️ 恢复后必须重启依赖 informer 缓存的组件（kube-state-metrics、各 operator）
```

## 相关文档

| 主题 | 位置 |
|---|---|
| 可观测性（Prometheus/Grafana/Loki） | [`observability/README.md`](../observability/README.md) |
| etcd 备份与恢复 | [`ops/etcd-backup/README.md`](../ops/etcd-backup/README.md) |
| GitOps / Argo CD | [`gitops/README.md`](../gitops/README.md) |
| 演练与事故报告 | [`docs/postmortems/`](../docs/postmortems/README.md) |
| 跨云节点接入实录 | [`docs/postmortems/2026-09-22-cross-cloud-node.md`](../docs/postmortems/2026-09-22-cross-cloud-node.md) |
