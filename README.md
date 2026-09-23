# 免费资源雷达

**输入你需要的资源，帮你找到能免费用上的方案。**

中文界面 · 免费额度写清楚 · 标注要不要绑卡/实名 · 标注国内能不能直接用

---

## 它解决什么问题

网上最好的免费资源清单是 [free-for-dev](https://github.com/ripienaar/free-for-dev)（13 万 star），
但对中文用户有两个硬伤：

1. **全英文**，1335 条只能靠 Ctrl+F 自己翻
2. **一条国内平台都没有** —— 智谱送 2000 万 tokens、阿里云百炼 100 万、腾讯混元 100 万（1 年有效）、
   讯飞 500 万…… 这些最实用的，它完全没收录

所以这个项目的定位是：**「我需要 X」→ 立刻看到有哪些免费方案、各自什么条件、有什么坑。**

## 产品功能

**搜索**（全中文，支持自然语言）

```
免费的大模型 API   → 18 条（自动分类到 AI 大模型 API）
免信用卡的         → 自动识别成筛选条件，返回不需要绑卡的
学生免费           → 学生专属福利：GitHub 学生包（含 DigitalOcean $200 + JetBrains 全家桶）
                      JetBrains 学生授权 / Azure for Students / 阿里云高校计划
图床 / 内网穿透    → 中文连写也能搜到（无需空格）
```

**我的领用记录**（最实用的功能）
国内平台的免费额度几乎都是限时的。真实痛点不是"找不到"，而是**一次领了七八家，过两个月全忘了，额度白过期**。
所以可以标记「我注册了」+ 填到期日，顶部显示倒计时，到期前自动发邮件提醒。

**链接巡检**（每天）
免费服务最容易"悄悄死掉"。系统每天自动巡一遍所有精选条目的官网，挂了就在页面上打标记。

**每周订阅**
一封邮件汇总：本周新收录 / 快过期的额度 / 链接挂了的条目。没内容就不发。

## 数据分两层

| 层 | 来源 | 条目 | 说明 |
|---|---|---|---|
| **精选** | `app/data_curated.py`（人工整理） | 88 | 国内平台为主，每条有中文额度、要求、注意事项、核对月份 |
| **收录** | 每 6 小时抓 free-for-dev | 1335 | 覆盖海外服务，靠中文分类 + 同义词表桥接 |

只做抓取会漏掉最有用的国内平台；只做人工整理又覆盖不全。所以两层都要。

## 技术架构

```
用户
  ↓
Cloudflare Tunnel（内网穿透，免公网 IP）
  ↓
ingress-nginx
  ↓
应用（2 副本，无状态）─────► PostgreSQL（共享存储）
  │                             ▲
  ├─ CronJob 采集（每 6h）──────┤
  ├─ CronJob 巡检（每天）───────┤
  └─ CronJob 周报（每周一）─────┘
  │
  └─► /metrics ──► Prometheus ──► Grafana
      span      ──► Tempo
      日志      ──► Loki

Git 推送 → GitHub Actions（测试 + 多架构镜像）
        → 回写镜像 tag 到仓库
        → Argo CD 自动同步到两个集群
```

**多集群 GitOps**：一个仓库，Argo CD 同时管理两个集群
- 东京集群（Oracle 免费 ARM 主控 + Micro worker）→ 用基础清单
- 阿里云集群（独立 k3s，1.6GB）→ 用 `overlays/aliyun`

## 可观测性三件套

| 组件 | 回答什么 |
|---|---|
| **Prometheus** | 出了什么事（指标异常） |
| **Loki** | 报了什么错（日志内容） |
| **Tempo** | **这一次请求慢在哪一环**（调用链耗时） |

告警走 Alertmanager → 163 邮箱。**额度到期提醒也是复用这条链路**（把业务状态变成指标再触发告警），省掉一套发信代码。

成本面板：把集群规模折算成「按量付费要多少钱」（约 ¥391/月，实际支付 ¥0）。

## 关键工程决策与踩过的坑

这部分是这个项目真正的值钱之处 —— **能说清"为什么这么做"和"踩过什么坑"**。

**1. SQLite → PostgreSQL 迁移**
应用原本是单文件 SQLite 挂 RWO 卷 → 跨节点跑不了多副本 → HPA 是摆设。
换 PG 后应用无状态，才谈得上水平扩容。迁移写了幂等的 `migrate` 命令，数据一条没丢。

**2. 存储适配层（`app/database.py`）**
同一套业务代码支持两种数据库，`db.connect()` 签名不变，业务代码一行没改。
`?`↔`%s`、自增主键写法等方言差异全在这一层消化。
> 踩坑：`lastrowid` 是 SQLite 方言，切库后报错 → 改用 `RETURNING id`（两边都支持）。
> 占位符替换必须**跳过引号内的问号**（那是数据），否则中文文案会炸。

**3. 多集群必须按能力配 overlay**
把同一份清单原样分发到两个集群，结果 1.6GB 那台被打满、sshd 都起不来。
复盘找到放大器：**CPU 高 → HPA 扩容 → 更耗资源 → CPU 更高**（崩前扩到了 5 个副本）。
→ **同一套清单 ≠ 同一套参数**。overlay 只改副本数和 HPA 上限，业务逻辑一行不动。
> 踩坑：overlay **不能放在 base 目录里**，否则 kustomize 报 `cycle detected`，什么都部署不出去。

**4. 调度约束要写进清单**
业务 Pod 被调度到 1GB 的 Micro（系统组件已占 92% 内存）→ 探针响应不了 → 一直重启 → CI 报超时 → 失败邮件。
用 `nodeAffinity` 排除它（用 affinity 而非 taint：taint 会连带影响没有 toleration 的 DaemonSet）。
> **靠"记得别调度过去"是不可靠的，必须写进清单。**

**5. 改 YAML 必须做语义检查**
连续踩了三个同源的坑，每个都让 GitOps 停摆：重复键（PyYAML 宽容、kustomize 拒绝）、
缩进错位、块被插错位置。→ 加了 `tools/validate-manifests.py` 并作为流水线步骤。

**6. 在国内的机器上拉境外镜像**
k3s 用**自己那份 containerd 配置**（`/etc/rancher/k3s/registries.yaml`），
不读 `/etc/containerd/certs.d`。不配的话连 pause 镜像都拉不下来。
> ghcr.io 的问题很隐蔽：`curl /v2/` 返回 401（看着正常），但镜像层下载走 GitHub CDN 会被卡。
> **必须真的拉 manifest 才能发现**。

**7. 到期提醒不自己写发信代码**
把业务状态变成 Prometheus 指标 → 告警规则 → Alertmanager 发邮件。零新增凭据、零新增发信路径，
还顺带拿到历史曲线。周报是内容邮件，才走 SMTP。

## 快速开始

```bash
pip install -r requirements.txt

python -m app.cli collect      # 抓一次上游（写入数据库）
uvicorn app.web:app --port 8000 # 起服务
pytest -q                       # 73 个测试
```

默认用 SQLite（无需数据库服务）；配 `RADAR_DB_URL` 即切 PostgreSQL。

## 目录结构

```
app/
  data_curated.py   人工精选数据（88 条）★ 核心资产
  search.py         中文检索层（同义词表 / 分类映射 / 标签换算）
  userdata.py       「我的领用记录」+ 到期计算
  linkcheck.py      官网链接巡检
  digest.py         每周订阅邮件
  database.py       存储适配层（SQLite / PostgreSQL）
  tracing.py        链路追踪（可选接入）
  web.py / metrics.py / collector.py / cli.py / db.py

k8s/                基础清单（Deployment / CronJob / PG / HPA / NetworkPolicy）
overlays/aliyun/    小机器专用 overlay（降副本 + 压 HPA 上限）
observability/      Prometheus / Grafana / Loki / Tempo 配置与面板
ops/                集群运维手册、etcd 备份
docs/postmortems/   故障演练与事故报告
tools/              清单校验脚本
```

## 线上

- 应用：`/` 搜索页 · `/changes` 上游变更历史
- 接口：`/api/search` · `/api/mine` · `/api/links` · `/api/curated`
- 监控：Grafana（业务指标 + 成本面板）· Alertmanager · Prometheus

> 免费政策随时会变。精选数据带 `reviewed` 字段记录核对月份，注册前请以官网为准。
