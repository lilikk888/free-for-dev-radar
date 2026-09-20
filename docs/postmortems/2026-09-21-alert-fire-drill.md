# 演练报告：告警 fire drill —— 验证告警真的会响

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-21 02:00 (GMT+8) |
| 环境 | OCI 东京 ARM 单节点集群，radar 应用 + kube-prometheus-stack |
| 类型 | 计划内演练 |
| 结果 | ✅ 两条关键告警**均从 pending → firing 并送达 Alertmanager** |
| 影响 | 无（演练任务跑完即删，指标已复位） |

## 1. 为什么要做这个演练

前一天刚踩过一个坑：[指标重复样本导致告警静默失效](2026-09-21-silent-alert-failure.md)。
那次事故最刺痛人的一点是 —— **告警写了、规则加载了、面板全绿，
但它从来不可能触发，而没有任何人知道。**

所以这次要补上运维里最容易被跳过的一步：**主动让它响一次**。

> **没验证过会响的告警，等于没有告警。**

## 2. 演练前状态

```
Pushgateway: radar_collect_success = 1     (正常)
Prometheus 活动告警: Watchdog(firing) + KubeAPIErrorBudgetBurn(pending)
```

## 3. 演练 A：真实采集失败 → `RadarCollectFailed`

**做法**：不动代码、不动规则，**制造一次真实的采集失败** ——
用同一个镜像起一个 Job，只把上游地址换成不可达的：

```yaml
# 关键就是这一行环境变量覆盖
env:
  - name: RADAR_SOURCE_URL
    value: http://127.0.0.1:1/upstream-is-down
  - name: RADAR_TIMEOUT
    value: "5"
```

**结果**：

```
Job 状态: 0/1 Error
日志: 采集失败: 抓取 http://127.0.0.1:1/upstream-is-down 失败:
      <urlopen error [Errno 111] Connection refused>（指标已推送: True）
Pushgateway: radar_collect_success 0     ← 从 1 变成 0
```

等待 `for: 1m` 后：

```
Prometheus:   RadarCollectFailed  state=firing  severity=critical  since=18:57:45
Alertmanager: RadarCollectFailed  state=active
```

✅ **完整链路打通**：真实失败 → 指标 → 规则求值 → 告警 firing → Alertmanager 收到。

> 顺带验证了 kube-prometheus-stack 自带的 `KubeJobFailed` 规则也正常触发了
> （Job 失败本来就会触发它）—— 说明 chart 默认规则集是活的。

## 4. 演练 B：`RadarSnapshotStale` —— 那条「不依赖 Pushgateway 的兜底」

这条告警最特殊：它**不依赖任何主动推送**，只看「距上次成功采集过了多久」。
就算 CronJob 整个起不来（被 suspend、镜像拉不动、上游不可达），
快照年龄也会自己涨上去。**它是整个告警体系里的最后一道防线**，
而前一天刚发现它自己是坏的（NaN 比较恒为 false）。

**做法**：不改表达式形态，只**临时把阈值和 `for` 调小**，让真实规则在可观测时间内触发：

```bash
kubectl -n radar patch prometheusrule radar --type=json -p '[
  {"op":"replace","path":"/spec/groups/0/rules/1/for","value":"10s"},
  {"op":"replace","path":"/spec/groups/0/rules/1/expr",
   "value":"radar_last_snapshot_age_seconds > 1"}
]'
```

**结果**：

```
Prometheus:   RadarSnapshotStale  state=firing  severity=critical  activeAt=18:02:15
              summary: radar 已超过 9 小时没有成功采集
Alertmanager: RadarSnapshotStale  state=active
```

✅ 表达式、标签、severity、告警文案、送达路径全部正确。

**随后还原**：`kubectl apply -f observability/manifests/radar-prometheusrule.yaml`

## 5. 遇到的两个小插曲

### 5.1 第一次只到 `pending`

第一次把 `for` 改成 `20s` 后等了 70 秒，状态还是 `pending`。

**原因**：改 PrometheusRule 之后，prometheus-operator 需要重新生成配置并让
Prometheus 热加载，这段传播延迟有几十秒。所以「等 70 秒」里真正在计时的可能只有十几秒。

**教训**：验证告警时，**等待时间要算上「规则生效延迟 + for 时长 + 求值间隔」**，
不能只等 `for`。第二次改成 `for=10s` 并等 150 秒，一次成功。

### 5.2 意外发现：`PrometheusDuplicateTimestamps` 长期 firing

演练过程中注意到这条告警一直在 firing，查下来的来源是：

```
sum by (job) (rate(prometheus_target_scrapes_sample_duplicate_timestamp_total[5m]))
  monitoring-kube-prometheus-prometheus      0.0333     ← 正好 = 每 30s 抓取周期丢 1 个
```

**是 Prometheus 抓自己**（self-scrape）时每个周期丢掉 1 个样本，速率精确等于抓取频率。

- **状态**：⚠️ **原因未完全定位，已记为待查项**（见 AI-11）
- 影响：warning 级、自指（self-referential），不影响业务
- 下一步排查方向：给 Prometheus 开 `--log.level=debug` 看具体是哪个指标重复；
  或检查 self-scrape 的 `honorLabels` / `honorTimestamps` 配置

> 这类「不难看但一直亮着」的告警最消耗注意力，长期会让团队对告警脱敏。
> 要么定位根因、要么明确降噪，**不能放着不管**。

## 6. 演练后的收敛确认

| 检查项 | 结果 |
|---|---|
| 演练 Job | ✅ 已删除 |
| `RadarCollectFailed` | ✅ 已 resolved（跑一次正常采集后 success 回到 1）|
| `RadarSnapshotStale` | ✅ 已 resolved |
| 6 条规则的 `for` | ✅ 全部还原为原值（1m/15m/30m/5m/10m/5m）|
| Pushgateway `radar_collect_success` | ✅ = 1 |
| 应用 | ✅ 200，1334 条 |
| 活动告警 | 只剩 `Watchdog`（设计如此）+ `PrometheusDuplicateTimestamps`（待查）|

## 7. Action Items

| 编号 | 改进项 | 优先级 | 状态 |
|---|---|---|---|
| AI-11 | 排查 `PrometheusDuplicateTimestamps` 自抓取的重复样本根因，或明确降噪 | 中 | 待办 |
| AI-12 | 把 fire drill 变成**定期动作**（建议每季度，或每次新增告警后必须做一次）| 高 | 待办 |
| AI-13 | Alertmanager 目前只有默认的 null receiver —— **告警只进 UI，不会通知到人**。要接真实通知渠道（邮件/webhook/IM）才算真正「会响」| 高 | 待办 |
| AI-14 | 把「新告警必须验证触发过一次」写进告警新增流程（可作为 PR checklist）| 中 | 待办 |

> **AI-13 是这次演练暴露出的真正短板**：我们证明了「告警能送到 Alertmanager」，
> 但 Alertmanager 配置里没有任何有效 receiver，所以它到此为止、
> **不会主动告诉任何人**。对个人项目影响不大（你会主动来看 Grafana），
> 但如果这套东西要给团队用，这一步必须补上。

## 8. 可复用的 fire drill SOP

```bash
# ---------- A. 制造真实业务失败（最推荐，最贴近真实） ----------
# 用同镜像起一个 Job，只改坏关键环境变量，不要改规则
kubectl -n <ns> apply -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata: { name: drill-fail, namespace: <ns> }
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: drill
          image: <同生产镜像>
          command: ["..."]
          env: [{ name: <关键变量>, value: <故意错的值> }]
YAML

# ---------- B. 验证 firing 与送达 ----------
# 等「规则生效延迟 + for 时长 + 求值间隔」，别只等 for
curl -s "http://localhost:9090/api/v1/alerts" | jq '.data.alerts[] | select(.labels.alertname|startswith("X")) | {alertname:.labels.alertname, state}'
curl -s "http://localhost:9093/api/v2/alerts" | jq '.[] | select(.labels.alertname|startswith("X")) | {alertname:.labels.alertname, state:.status.state}'

# ---------- C. 收敛与复位 ----------
kubectl -n <ns> delete job drill-fail
<跑一次正常任务，让指标回到健康值>
curl -s "http://localhost:9090/api/v1/alerts"   # 确认已 resolved
```

**注意区分两种 drill**：

| 方式 | 验证什么 | 风险 |
|---|---|---|
| 制造真实失败（A）| 指标 → 规则 → 告警 全链路 | 要记得复位 |
| 临时调小阈值 | 规则表达式本身是否正确 | 必须从仓库文件还原，别手改 |
