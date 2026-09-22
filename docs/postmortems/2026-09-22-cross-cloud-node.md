# 把阿里云广州节点接入东京集群（跨云 K8s）

> 日期：2026-09-22 · 影响：无生产影响（新增能力）· 类型：架构变更 + 排障实录

## 一、起因

集群只有一台东京 ARM 主控，一是**不像"集群"**（没有真正的多节点调度），
二是**学不到多节点才有的东西**（Pod 漂移、跨节点网络、失效域隔离）。

手上可选的三台机器：

| 候选 | 规格 | 优势 | 劣势 |
|---|---|---|---|
| Oracle Micro（东京） | 1/8 OCPU / 1 GB | 同子网，零网络改造 | 规格极小 |
| **阿里云 ECS（广州）** | 2 vCPU / 1.6 GB | CPU 是 Micro 的 16 倍、磁盘快 | 跨云跨地域，要自己搭隧道 |

**两台都要**：Micro 提供"同一内网多节点"的对照，阿里云提供"跨云多节点"的真场景。

## 二、最终架构

```
        Oracle 东京（10.0.2.0/24，同一 VPC 子网）
        ┌─────────────────────────────────────────┐
        │  free-arm-2c12g   10.0.2.134  arm64  控制面 │
        │  instance-...-1251 10.0.2.63  amd64  worker │
        └────────────────┬────────────────────────┘
                         │  WireGuard  UDP 51820  MTU 1380
                         │  隧道网段 10.9.0.0/30  延迟 165 ms
        ┌────────────────┴────────────────────────┐
        │  阿里云 广州  10.9.0.2  amd64  worker    │
        └─────────────────────────────────────────┘
                         └── Calico VXLAN (UDP 4789) 跑在隧道之上
```

关键设计决策（每条都有理由）：

| 决策 | 理由 |
|---|---|
| 用 WireGuard 而不是把 apiserver 暴露公网 | kubelet 与 apiserver 的通信含凭证与证书，绝不能裸露在公网 |
| 阿里云节点连 `10.0.2.134:6443` 而不是隧道 IP | 该 IP 已在 apiserver 证书 SAN 中，**省掉一次证书操作**（我原本打算改证书，中途放弃） |
| 隧道 `AllowedIPs` 带 `10.0.2.0/24` | 让阿里云能直连主控的 VPC 内网地址 |
| 全局 MTU 降到 1280 | VXLAN 包 1450 装不进 MTU 1380 的隧道 |
| `IP_AUTODETECTION_METHOD=can-reach=10.0.2.134` | 一条设置同时满足三种节点：主控/Micro 选 `ens3`，阿里云选 `wg0` |
| 阿里云节点打 `PreferNoSchedule` | 165 ms 延迟不适合承载交互式负载，但不影响 DaemonSet |

## 三、排障实录：四个坑，有三次误判

这次真正的教训不是"网络难搞"，而是**我在同一个错误假设上撞了三次**。

### 误判一：把"安全列表没放行"当成默认答案

`kubeadm join` 报 `dial tcp 10.0.2.134:6443: connect: no route to host`。
我第一反应是 OCI 安全列表 —— **但错误码已经告诉我不是**。

```
No route to host = 收到了 ICMP host-prohibited = 有设备 REJECT
安全列表丢包是静默的      = 表现为 timeout
```

真因：主控 `INPUT` 里的 REJECT 规则只放行了 `10.0.0.0/16`，
阿里云从 `10.9.0.2` 过来（隧道网段）**不在放行范围内** → 被 REJECT。

**教训**：错误码是免费的信息。`REJECT` 和 `DROP` 是两种完全不同的故障层，
先分清再动手，能省掉一轮错误的排查。

### 误判二：忘了 Pod 的流量要过 **FORWARD**

放行 INPUT 之后，**宿主机之间**通了（`ping 主控 Pod IP` 成功），
但**Pod 之间仍然 100% 丢包**。

差异点：宿主机发出的包走 `OUTPUT`，Pod 发出的包要走 **`FORWARD`**。
而那台 Micro 的 `FORWARD` 链**也有一条镜像预置的 REJECT**，
且它排在 Calico 的 `ACCEPT`（mark 0x10000）规则**之前** ——
所以 Calico 判定放行的包，还没轮到那条 ACCEPT 就被 REJECT 了。

```
Chain FORWARD
6  REJECT  all  --  ... reject-with icmp-host-prohibited   ← 在这
7  ACCEPT  all  --  ... mark match 0x10000/0x10000          ← Calico 的规则在这，永远轮不到
```

**排查手法（值得复用）**：用 `tcpdump` 在两端抓包，
如果**对端网卡一个包都没收到**，就说明问题在**本机**（转发/封装阶段），
不要再去怀疑链路。

### 误判三：用错误的命令"验证"镜像加速

给阿里云配了国内镜像源后，我用 `ctr images pull` 测试，仍然超时，
差点得出"镜像源不可用"的错误结论。

真因：**`ctr` 这个命令行工具不走 CRI 的镜像配置**（`certs.d` 是给 CRI 用的）。
必须加 `--hosts-dir /etc/containerd/certs.d` 才等价于 kubelet 的行为。

**教训**：验证工具的路径必须和被测系统一致，否则测的是另一件事。

### 坑四（自己写错配置）：WireGuard 只配了一端的端口

现象：主控 `wg show` 显示 `transfer: 0 B received, 35.99 KiB sent` ——
一直在发，收不到任何回应。

对端抓包却能看到包**已经送达**：

```
eth0  In  IP 193.123.164.23.51820 > 172.29.26.94.51820: UDP, length 148
```

真因：阿里云侧的 WireGuard 配置里**没写 `ListenPort`**，
于是它监听在**随机端口 42399**，主控发到 51820 的包到了却没人接。

**教训**：双向隧道两端都要显式声明端口，别指望默认值一致。

## 四、过程中的一次失控（诚实记录）

### 改 apiserver 证书 SAN 把集群配置写坏

为了让阿里云节点能用隧道 IP 访问 apiserver，我打算给证书加 SAN `10.9.0.1`：

```bash
sudo python3 -c "
s = s.replace('apiServer: {}', 'apiServer:\n  certSANs:\n  - 10.9.0.1')
"
```

这段代码经过 **本地 shell → SSH → 远程 shell** 三层转义后，
`\n` 变成了**字面字符**，写进 ConfigMap 的是一段非法 YAML：

```
apiServer:/n  certSANs:/n  - 10.9.0.1      ← 一行，不是三行
```

后果：`kubeadm certs renew` 报 `yaml: line 2: mapping values are not allowed`。

处置：
1. 用「**写脚本文件再 `sudo` 执行**」的方式修复 ConfigMap（恢复为 `apiServer: {}`）
2. `kubeadm config validate` 校验通过
3. **最终放弃改证书** —— 改用「让隧道把 `10.0.2.0/24` 也路由过来」的方案，
   复用证书里已有的 `10.0.2.134`，一行证书都不用动

**教训**：
- 多层转义的 `python -c` 不要用，改成写脚本文件
- 改关键配置前先备份（当时已备份 `/etc/kubernetes/pki`，但没备份 ConfigMap）
- **遇到"要改基础组件配置"的方案时，先停下来找有没有绕开的办法** —— 这次绕过之后
  整个操作从"高风险证书操作"变成"改一行路由配置"

### 附带发现的假故障

apiserver 重启后 20~30 秒内，`kubectl get nodes` 会返回 **Forbidden**：

```
User "kubernetes-admin" cannot list resource "nodes"
```

这不是权限丢了 —— 是 apiserver 的 **RBAC informer 缓存还没同步**，
等到 30 秒后自己恢复。**别在这个窗口里做任何判断。**

## 五、验证结果（实测，非推测）

```
$ kubectl get nodes
free-arm-2c12g            Ready   control-plane   arm64   东京   10.0.2.134
instance-20260902-1251    Ready   <none>          amd64   东京   10.0.2.63
iz7xvdabk2g1oasakx27fnz   Ready   <none>          amd64   广州   10.9.0.2

# 跨云 Pod 到 Pod（阿里云 Pod → 东京主控上的 coredns）
$ kubectl exec nettest -- ping -c 4 192.168.48.3
4 packets transmitted, 4 packets received, 0% packet loss
round-trip min/avg/max = 165.555/165.629/165.760 ms

# 跨云 DNS 解析
$ kubectl exec nettest -- nslookup kubernetes.default.svc.cluster.local
Address: 10.96.0.1          ← 跨云解析成功

# 监控盲区检查
kubelet 3 个节点 = up，node-exporter 3 个节点 = up，0 个 DOWN target
```

**延迟 165 ms 是可以接受的**：worker 节点与 apiserver 之间是低频心跳（node lease / 状态上报），
不是高频 RPC；这个数字对 etcd 一致性没有影响（worker 不参与 etcd）。

## 六、还做了什么（顺带补齐的能力）

| 项 | 说明 |
|---|---|
| **metrics-server** | 之前没装 → `kubectl top` 不可用、无法做 HPA。现已装（`--kubelet-insecure-tls` 是 kubeadm 集群的必需参数） |
| **NetworkPolicy** | `radar` 命名空间默认拒绝入站，只放行 ingress-nginx 与 Prometheus —— 用 Calico 的策略引擎落地"最小暴露面" |
| **HPA 演示** | `demos/hpa/`，独立命名空间，演示基于 CPU 的自动扩缩容 |
| **运维手册重写** | `ops/cluster-runbook.md`：拓扑、端口、四个坑、Micro 的正确用法 |

## 七、留下的结论

1. **跨云 K8s 可行，但不是免费的**：多了一层隧道 = 多一个 MTU 约束、
   多一个网卡选择问题、多一个故障域（隧道断了节点就失联）。
   单集群跨云适合**学习与演练**，生产上更常见的做法是「多集群 + GitOps 分发」。
2. **云主机的"预置防火墙"是最容易被忽略的一层**：OCI 镜像在 `INPUT` 和 `FORWARD`
   各埋了一条 REJECT，这次全踩到了。**任何新机器接入前，先 dump 一次 iptables。**
3. **1 GB 机器能做 worker，但只能承载 DaemonSet 级别的负载**，
   而且必须关掉系统自带的自动更新任务（snapd / apt-daily / unattended-upgrades）——
   实测那些才是 1/8 OCPU 机器上真正的 CPU 消耗大户。
