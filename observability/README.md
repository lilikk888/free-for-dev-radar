# 可观测性（Observability）

radar 应用跑在一个 kubeadm 单节点集群上，这一层负责回答"集群现在健康吗、刚才发生了什么"。

## 装了什么

| 组件 | 版本 | 作用 | 命名空间 |
|---|---|---|---|
| kube-prometheus-stack | 91.4.1 | Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics | `monitoring` |
| Loki | 7.3.0 | 日志聚合（SingleBinary 模式 + filesystem 存储） | `monitoring` |
| Promtail | 6.17.1 | DaemonSet，采集所有节点容器日志推给 Loki | `monitoring` |

**指标 + 日志都有**，这是后面做故障演练的前提：写 Postmortem 时你既要说"什么时候开始异常的"（指标），
也要说"当时报了什么错"（日志）。只有其中一份都是空谈。

## 访问

Grafana 挂在 **`/grafana`** 子路径下（不是 `/`）：

```
https://<你的隧道域名>/grafana
```

> ⚠️ 为什么不用 `/`：radar 应用的 Ingress 已经占了 catch-all 的 `/`。
> **同一个集群里只能有一个 catch-all `/` 规则**，两个会互相抢路由。

### 拿 Grafana 管理员密码

```bash
kubectl -n monitoring get secret monitoring-grafana \
  -o jsonpath="{.data.admin-password}" | base64 -d; echo
```

用户名 `admin`。

### 数据源

Grafana 启动时由 sidecar 自动注册三个数据源，**不需要手动在 UI 里配**：
Prometheus、Loki、Alertmanager。Loki 是通过 `manifests/loki-datasource.yaml`
里带 `grafana_datasource: "1"` 标签的 ConfigMap 被自动发现的。

## 安装步骤

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

# 1) 指标
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace --version 91.4.1 \
  -f observability/values/kube-prometheus-stack.yaml

# 2) 日志
helm install loki grafana/loki -n monitoring --version 7.3.0 \
  -f observability/values/loki.yaml
helm install promtail grafana/promtail -n monitoring --version 6.17.1 \
  -f observability/values/promtail.yaml

# 3) 让 Grafana 认识 Loki
kubectl apply -f observability/manifests/loki-datasource.yaml

# 4) 开放控制面组件的 metrics 端口（kubeadm 默认不给抓）
sudo NODE_IP=<节点内网IP> bash observability/scripts/enable-control-plane-metrics.sh
```

> `loki` 这个 release 名不能随便改：chart 的 fullname 逻辑在 release 名包含 chart 名时会省略后缀，
> 所以叫 `loki` 才能得到 `http://loki:3100` 这个地址（Grafana 数据源和 Promtail 都依赖它）。

## 两个必踩的坑（都是 kubeadm 特有的）

### 坑一：控制面指标默认抓不到

kubeadm 装完的集群，Prometheus 里这几个 job 会一直 **DOWN**：

| job | 端口 | 原因 |
|---|---|---|
| kube-scheduler | 10259 | `--bind-address=127.0.0.1` |
| kube-controller-manager | 10257 | `--bind-address=127.0.0.1` |
| kube-etcd | 2381 | `--listen-metrics-urls=http://127.0.0.1:2381` |
| kube-proxy | 10249 | ConfigMap 里 `metricsBindAddress: ""`（空 = 只监听 localhost） |

**修法用 `scripts/enable-control-plane-metrics.sh`**（幂等、先备份）。

关键细节：scheduler / controller-manager 要绑 **`0.0.0.0` 而不是节点 IP**。
因为 kubelet 的 liveness/readiness 探针打的是 `127.0.0.1:probe-port`，
只绑节点 IP 会让 localhost 不通 → 静态 Pod 无限重启。

脚本用 sudo 跑，但 root 没有 `~/.kube/config`，所以脚本内部会显式
`export KUBECONFIG=/etc/kubernetes/admin.conf`，否则 kubectl 会去连 `localhost:8080` 然后报 refused。

回滚：
```bash
sudo cp /home/ubuntu/kubeadm-manifest-backup/*.yaml /etc/kubernetes/manifests/
kubectl -n kube-system rollout restart daemonset kube-proxy
```

### 坑二：Grafana 开了子路径后 `/metrics` 也移位了

Grafana 配了 `serve_from_sub_path = true` 之后，**所有端点都会挂到 `/grafana` 下**，
连 metrics 端点都从 `/metrics` 变成 `/grafana/metrics`（直接请求 `/metrics` 返回 301）。

不改的话，Prometheus 里 `monitoring-grafana` 这个 target 永远 DOWN：

```
"http://<podIP>:3000/metrics" → 301
"http://<podIP>:3000/grafana/metrics" → 200
```

修法是在 values 里显式指定：
```yaml
grafana:
  serviceMonitor:
    path: /grafana/metrics
```
（chart 自己的注释就提示了这点：*"Path to use for scraping metrics. Might be different if
server.root_url is set in grafana.ini"*）

## 资源占用（2 OCPU / 12 GB 单节点实测）

| 项 | 数值 |
|---|---|
| 整套监控栈内存 | ≈ 1.0 GB |
| Prometheus 保留期 | 15 天 / 8 GiB PVC |
| Loki 保留期 | 7 天 / 10 GiB PVC |
| 磁盘占用 | 45 G 中用了约 10 G |

## 顺带调整：保活脚本从 3 GB 缩到 1 GB

这个实例是 Oracle Always Free，**闲置会被回收**。官方判定规则是 7 天窗口内三条同时成立才算闲置：

- CPU 95 分位 < 20%
- 网络 < 20%
- **内存 < 20%（仅 A1/ARM 形状）**

装上监控栈之后，真实业务本身就贡献了约 23% 的内存占用。但**只多 3 个点、余量太薄**，
所以把保活脚本的 `--vm-bytes` 从 `3G` 降到 `1G`：

| | 之前 | 之后 |
|---|---|---|
| 保活内存 | 3.0 GB | 1.0 GB |
| 保活 CPU | 10.1% | 1.8% |
| 总内存利用率 | 48% | **30.9%** |
| 可用内存 | 6188 MB | **8247 MB** |

结论：**释放 2 GB 给真实业务，同时把内存利用率稳定保持在阈值之上 10.9 个点。**

> 注意：内存利用率是算上真实业务的。如果哪天把监控栈整个删了，记得把保活调回 3G 或更高。
