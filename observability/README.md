# 可观测性（Observability）

radar 应用跑在一个 kubeadm 单节点集群上，这一层负责回答"集群现在健康吗、刚才发生了什么"。

## 装了什么

| 组件 | 版本 | 作用 | 命名空间 |
|---|---|---|---|
| kube-prometheus-stack | 91.4.1 | Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics | `monitoring` |
| Loki | 7.3.0 | 日志聚合（SingleBinary 模式 + filesystem 存储） | `monitoring` |
| Promtail | 6.17.1 | DaemonSet，采集所有节点容器日志推给 Loki | `monitoring` |
| Prometheus Pushgateway | 3.8.0 | 短命任务（CronJob）的指标中转站 | `monitoring` |

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

## 把业务指标接进来（这个项目自己的指标）

上面的东西监控的是「集群」，看不出「项目在不在干活」。业务指标由应用自己暴露，
但**分两条路走**，因为常驻进程和短命进程的暴露方式根本不同：

| 来源 | 谁产生 | 怎么被采集 | 指标 |
|---|---|---|---|
| Web Pod 的 `/metrics` | 常驻的 uvicorn 进程 | Prometheus 按 30s **拉** | `radar_entries_total`、`radar_changes_total{change_type}`、`radar_snapshots_total`、`radar_last_snapshot_age_seconds`、`radar_http_requests_total{method,path,status}` |
| Pushgateway | CronJob（跑几秒就退出） | 任务**推**到 Pushgateway，Prometheus 再拉 Pushgateway | `radar_collect_success`、`radar_collect_duration_seconds`、`radar_collect_entries`、`radar_collect_changes{change_type}` |

> **为什么必须有 Pushgateway**：Prometheus 是拉模型，按固定间隔去抓目标。
> CronJob 每 6 小时才跑一次、每次几秒就退出 —— 抓的时候进程早没了。
> 这是监控批处理任务的经典问题，Pushgateway 就是标准答案。
>
> 代价：Pushgateway 上的指标不会自动过期，所以它只适合放「最近一次运行结果」，
> 不能当计数器长期累计。

### 实现要点

- `app/metrics.py` 里用了**两个独立的 CollectorRegistry**（`WEB_REGISTRY` / `JOB_REGISTRY`），
  而不是默认的全局注册表 —— 否则推送时会把 Web 的指标一起推上 Pushgateway。
- 库里的状态用**自定义 Collector** 暴露：每次 Prometheus 抓取时才去查 SQLite，
  所以指标永远是最新的，也不用在 Web 进程里跑后台任务。
- HTTP 请求计数的 `path` 标签用**路由模板**而不是原始路径，
  否则随便一个扫描器打一堆 `/xxx` 进来就能把 Prometheus 的标签基数打爆。
- CronJob **采集失败时也会推指标**（`radar_collect_success=0`）——
  否则「任务挂了」这件事在监控里完全看不见，等于没监控。

### 仪表盘与告警

- 仪表盘：`manifests/grafana-dashboard-radar.yaml`（ConfigMap，sidecar 自动导入），
  Grafana 里搜 **radar · 业务指标**
- 告警规则：`manifests/radar-prometheusrule.yaml`，6 条：

| 告警 | 触发条件 | 严重度 |
|---|---|---|
| `RadarCollectFailed` | 最近一次采集失败 | critical |
| `RadarSnapshotStale` | 超过 9 小时没成功采集（**不依赖 Pushgateway 的兜底**）| critical |
| `RadarEntriesDropped` | 条目数比一天前骤降 > 20% | warning |
| `RadarCollectSlow` | 采集耗时 > 60s | warning |
| `RadarHttpErrorRate` | 5xx 占比 > 5% | warning |
| `RadarTargetDown` | Prometheus 抓不到 `/metrics` | warning |

`RadarSnapshotStale` 值得单独说：就算 CronJob 整个起不来（被 suspend、镜像拉不动、
上游不可达），快照年龄也会一直涨，所以它是最后一道保险，不依赖任何主动推送。

### 安装

```bash
helm install pushgateway prometheus-community/prometheus-pushgateway \
  -n monitoring --version 3.8.0 -f observability/values/pushgateway.yaml

kubectl apply -f observability/manifests/radar-servicemonitor.yaml
kubectl apply -f observability/manifests/radar-prometheusrule.yaml
kubectl apply -f observability/manifests/grafana-dashboard-radar.yaml

# 让 CronJob 知道往哪推（已写在 k8s/cronjob.yaml 的环境变量里）
#   RADAR_PUSHGATEWAY_URL=http://pushgateway.monitoring.svc.cluster.local:9091

# 手动触发一次采集，验证指标推到 Pushgateway
kubectl -n radar create job --from=cronjob/radar-collect manual-collect-$RANDOM
```

> ServiceMonitor / PrometheusRule 是 Prometheus Operator 的 CRD，只能在装了
> kube-prometheus-stack 的集群里 apply。所以它们刻意放在 `observability/` 而不是 `k8s/`：
> `k8s/` 是应用本身（本地 kind 也能跑），这些是「平台如何接入应用」。

## 四个必踩的坑

### 坑一：控制面指标默认抓不到（kubeadm 特有）

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

### 坑三：Helm 键名写错不会报错，只会**静默忽略**

kube-prometheus-stack 里同一个组件有**两套键**，长得很像但作用完全不同：

| 键 | 是什么 |
|---|---|
| `kubeStateMetrics` | 只有 `enabled` 这类开关 |
| **`kube-state-metrics`** | 子 chart 的真实配置（`resources` / 探针 / …） |
| `nodeExporter` | 只有 `enabled` |
| **`prometheus-node-exporter`** | 子 chart 的真实配置 |

写错键名时 `helm install/upgrade` **不会报任何错**，值被直接丢掉。
本项目第一版就把 `resources` 写在了 `kubeStateMetrics` 下，结果：

```
$ kubectl -n monitoring get pod -l app.kubernetes.io/name=kube-state-metrics \
    -o jsonpath="{.items[0].spec.containers[0].resources}"
{}          ← 空的！
```

没有资源保障 + 机器正在同时启动一堆组件 → kube-state-metrics 同步缓存变慢 →
`/livez` 返回 503 → 默认探针（5s 超时、连失 3 次）把它连着杀了 4 次：

```
Warning  Unhealthy  kubelet  Liveness probe failed: HTTP probe failed with statuscode: 503
Normal   Killing    kubelet  Container kube-state-metrics failed liveness probe, will be restarted
```

**修法**：写到正确的键下，并给单节点环境放宽探针。

```yaml
kube-state-metrics:
  resources:
    requests: {cpu: 20m, memory: 96Mi}
    limits:   {memory: 320Mi}
  livenessProbe:
    httpGet: {path: /livez, port: http}
    initialDelaySeconds: 20
    periodSeconds: 15
    timeoutSeconds: 10
    failureThreshold: 6
```

**教训**：Helm values 写完后一定要回读实际对象验证，不能只看 `helm upgrade` 成功：

```bash
kubectl -n monitoring get pod -l app.kubernetes.io/name=kube-state-metrics \
  -o jsonpath="{.items[0].spec.containers[0].resources}"
```

同理，排查 Pod 异常重启时**先看 `describe` 里的 Last State 和探针失败记录**，
别一上来就怀疑代码。

### 坑四：浏览器翻译会把 Grafana 首页搞崩

现象：打开 Grafana 首页直接白屏报错

```
发生了意外的错误
NotFoundError：未能在"Node"上执行"insertBefore"：新节点要插入的节点不是该节点的子节点
```

**判据**：如果报错堆栈里的函数名也被翻成了中文（`Suspense` → "悬疑现场"、`Main` → "主馆"…
这种机器翻译痕迹），那就基本可以确定是**浏览器的网页翻译**在捣乱。

原因：浏览器翻译是**直接改 DOM 文本节点**实现的，而 Grafana 首页是 React + SVG，
React 做节点比对时发现原来的节点不见了 → `insertBefore` 抛 NotFoundError。
这类问题不只 Grafana 有，任何重度 React 应用都可能中招。

**两种解法**：

1. **（推荐）关掉这个站点的翻译**：地址栏右侧翻译图标 → "不翻译此网站" / "显示原文"
2. **让 Grafana 原生显示中文**，这样你就不需要翻译了：

```yaml
grafana:
  grafana.ini:
    users:
      default_language: zh-Hans
```

> Grafana 官方汉化**并不完整**（只覆盖部分界面），但配了之后就不需要浏览器翻译，
> 也就从根上避免了这个问题。

**教训**：前端报了「DOM 节点找不到」这类错，先怀疑浏览器插件（翻译 / 广告拦截）
在改 DOM，再去查应用本身的 bug。

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
