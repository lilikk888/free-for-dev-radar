# 事故报告：监控指标静默失效，最关键的告警从未可能触发

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-21 01:37 (GMT+8) |
| 环境 | OCI 东京 ARM 单节点集群，radar 应用 |
| 类型 | **意外事故**（自己引入，自己发现） |
| 发现方式 | **人肉读 `/metrics` 原始输出** |
| 结果 | ✅ 已修复并加防回归测试 |
| 影响范围 | 无人受直接影响（个人项目），但**监控在最需要的时候是失效的** |

## 1. 现象

给 radar 接入业务指标后，随手把 `/metrics` 的输出打出来看，发现异常：

```
radar_entries_total 0.0                    ← 同一个「指标名+标签」出现了两次
radar_entries_total 1334.0
radar_last_snapshot_age_seconds NaN        ← 非法值
radar_last_snapshot_age_seconds 4833.903387069702
```

`radar_entries_total` 出现两次已经很可疑，更刺眼的是那个 **NaN**。

## 2. 根因

写自定义 Collector 时用了这个写法：

```python
entries = GaugeMetricFamily(
    "radar_entries_total", "最近一次快照解析出的条目总数", value=0   # ⚠️ 这里
)
...
entries.add_metric([], latest["entry_count"])   # 又加了一次
```

`GaugeMetricFamily.__init__` 的实现是：

```python
if value is not None:
    self.add_metric(labels or [], value)
```

**传了 `value=0` 就等于先加了一个值为 0 的样本**，后面再 `add_metric()` 就变成
「同名同标签两个样本」。`radar_last_snapshot_age_seconds` 上的 `value=float("nan")` 更是
直接暴露了个 NaN 出去。

这违反 Prometheus 暴露规范：**同一个 `指标名{标签集}` 在一次抓取里只能出现一次。**

## 3. 真实影响（比格式问题严重得多）

一开始以为「Prometheus 会拒绝抓取，target 变成 DOWN」——查了才发现**没那么简单**：

```
job: radar    health: up    error: -
```

**Prometheus 容错处理了，target 显示 up，但数据是错的。** 查询实际存储值：

| 指标 | 存下来的值 | 应该是 |
|---|---|---|
| `radar_entries_total` | **0** | 1334 |
| `radar_last_snapshot_age_seconds` | **NaN** | ~4834 |

也就是说它保留了那个默认样本，把真实值丢了。

### 连锁后果：告警静默失效

| 告警 | 表达式 | 为什么永远不会触发 |
|---|---|---|
| `RadarSnapshotStale` | `radar_last_snapshot_age_seconds > 9*3600` | 值是 **NaN**，任何与 NaN 的比较都返回 false |
| `RadarEntriesDropped` | `radar_entries_total < (... offset 1d) * 0.8` | 值恒为 0，0 < 0 为 false |
| 仪表盘「当前条目数」 | 直接展示 `radar_entries_total` | 永远显示 0 |

**这是最难发现的一类监控故障：监控面板是绿的、target 是 up、没有报错，
但它在最需要报警的时候完全不会响。**

`RadarSnapshotStale` 本来是整个监控体系里刻意设计的「不依赖 Pushgateway 的兜底」——
结果它自己就是坏的。**兜底方案本身没有被兜底。**

## 4. 为什么没在更早发现

三个环节都漏了：

1. **代码写完没读原始输出**：只看了 `curl` 返回 200，没细看内容格式
2. **测试只断言"包含某个字符串"**：`assert "radar_entries_total 8.0" in body` 会通过，
   因为字符串确实在里面（多出来的 `radar_entries_total 0.0` 不影响这个断言）
3. **没有校验 target 的实际数值**：只看了 target 是不是 up，
   没去看「up 的 target 传上来的值对不对」

**核心教训：`up == 1` 只代表「能抓取」，不代表「抓到的数据是对的」。**

## 5. 修复

```python
# 不传 value=，只用 add_metric()
entries = GaugeMetricFamily("radar_entries_total", "最近一次快照解析出的条目总数")
ts_family = GaugeMetricFamily("radar_last_snapshot_timestamp_seconds", "...")
age = GaugeMetricFamily("radar_last_snapshot_age_seconds", "...")

if latest is not None:
    entries.add_metric([], latest["entry_count"])
    parsed = _parse_iso(latest["fetched_at"])
    if parsed is not None:
        ts_family.add_metric([], parsed)
        age.add_metric([], max(0.0, time.time() - parsed))
else:
    entries.add_metric([], 0)
```

补充决策：**从未采集过时刻意不输出 age / timestamp 指标**，而不是输出 NaN 或 0。
因为「没有数据」和「数据是 0」是两件事，用 `absent()` 报警才准确。

```yaml
- alert: RadarNeverCollected
  expr: absent(radar_last_snapshot_age_seconds)
  for: 15m
```

## 6. 防回归测试

加了两个测试，直接针对这类坑：

```python
def test_no_duplicate_samples(client):
    """扫描 /metrics 文本，找出「同名同标签」的重复样本。"""
    body = client.get("/metrics").text
    seen, duplicates = set(), set()
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        key = line.rsplit(" ", 1)[0]      # 去掉数值，只留 指标名{标签}
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    assert not duplicates, f"出现重复样本（Prometheus 会拒绝抓取）: {sorted(duplicates)}"


def test_no_nan_or_inf_samples(client):
    """NaN / Inf 不是合法的暴露值。"""
    ...
```

## 7. Action Items

| 编号 | 改进项 | 优先级 | 状态 |
|---|---|---|---|
| AI-7 | **写指标代码后必须读一次 `/metrics` 原始输出**，不能只看 HTTP 200 | 高 | 已纳入习惯 |
| AI-8 | 新指标上线后，**查一次 Prometheus 里的实际数值**，确认和预期一致（不是只看 target up）| 高 | 已纳入习惯 |
| AI-9 | 给关键告警做「**故意让它触发一次**」的验证（fire drill），确认告警链路真的通 | 高 | 待办 |
| AI-10 | 把「指标正确性」加进 CI：除了单元测试，部署后跑一次冒烟断言（比如 `radar_entries_total > 0`）| 中 | 待办 |

> AI-9 是最重要的一条。这次事故的本质是「告警从来没被验证过会响」。
> **没验证过会响的告警，等于没有告警。**

## 8. 一句话总结

> 写监控代码时，**HTTP 200 和 `target up` 都不是正确性证明**。
> 必须读原始输出、必须查实际存储值、必须让告警真的响一次。
