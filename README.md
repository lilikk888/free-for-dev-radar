# 免费资源雷达

**输入你需要的资源，帮你找到能免费用上的方案。**

中文界面 · 免费额度写清楚 · 标注要不要绑卡/实名 · 标注国内能不能直接用

---

## 为什么做这个

网上最好的免费资源清单是 [free-for-dev](https://github.com/ripienaar/free-for-dev)（13 万 star），
但它有两个对中文用户很致命的问题：

1. **全英文**，1300+ 条只能靠 Ctrl+F 自己翻
2. **一条国内平台都没有** —— 智谱送 2000 万 tokens、阿里云百炼 100 万、
   腾讯混元 100 万（有效期 1 年）、讯飞 500 万……这些它完全没收录

而中文圈只有零散的博客文章在做静态汇总，**没有一个能用的中文查询工具**。

所以这个项目的定位是：**「我需要 X」→ 立刻看到有哪些免费方案、各自什么条件、有什么坑。**

## 它长什么样

首页只有一个搜索框，下面按分类铺开。搜索支持自然语言：

| 你这样搜 | 它这样理解 |
|---|---|
| `免费的大模型 API` | 分类命中 AI 大模型 API，返回智谱/百炼/Gemini 等 18 条 |
| `免信用卡的` | **识别为筛选条件**（不是关键词），返回 48 条不需要绑卡的 |
| `免信用卡的图床` | 筛选（免信用卡）+ 检索（图床）→ 3 条 |
| `国内能用的数据库` | 筛选（国内直连）+ 检索（数据库） |
| `gpu` / `图床` / `内网穿透` | 中英双语都能搜 |

每条结果卡片包含五个中国用户最关心的字段：

- **免费额度**：具体数字，不是「有免费版」这种废话
- **要求**：免信用卡 / 需绑卡 / 需实名（一眼看出注册门槛）
- **国内可用性**：国内直连 / 部分可用 / 需梯子
- **有效期**：长期免费 / 限时赠送
- **注意**：这条最容易踩的坑（例如「12 个月到期后会自动转按费」）

## 数据分两层（这是设计核心）

| 层 | 来源 | 条目 | 说明 |
|---|---|---|---|
| **精选** | `app/data_curated.py`（人工整理） | 59 条 | 国内平台为主，每条都写了中文额度、要求、注意事项 |
| **收录** | 定时抓取 free-for-dev | 1300+ | 覆盖海外服务，靠中文分类 + 同义词表桥接，英文原文折叠显示 |

只做抓取会漏掉最有用的国内平台；只做人工整理又覆盖不全。
所以两层都要：**精选优先展示，收录作为补充**（页面里折叠在「展开 N 条英文条目」里）。

> ⚠️ 免费政策随时会变。精选数据带 `reviewed` 字段记录核对月份，
> 页面底部也提示了「注册前请以官网为准」。

## 技术栈与工作方式

```
K8s CronJob（每 6 小时）              用户
      │                                │
      ▼                                ▼
 抓 free-for-dev.md ──► SQLite ◄── FastAPI（中文检索层）
                          ▲                │
                          │                ▼
                   变更检测/快照      中文网页 + JSON API
                          │
                          ▼
             /metrics ──► Prometheus ──► Grafana / 邮件告警
```

- **应用**：Python 3.13 + FastAPI + SQLite（单文件，挂 PVC）
- **检索层**：`app/search.py` —— 中文同义词表 + 分类映射 + 标签换算
- **部署**：镜像由 GitHub Actions 构建，Argo CD 从仓库同步（GitOps）
- **可观测**：Prometheus 抓业务指标，Grafana 出图，异常走 163 邮件告警
- **灾备**：etcd 定时快照 + 加密 + 异机留存

顺带保留的能力：**上游清单变更检测**（原「变更雷达」视图，见 `/changes` 页面）。
它不是主功能了，但作为「数据采集 + 变更对比」的工程演示仍然有价值。

## 本地跑

```bash
pip install -r requirements.txt

# 抓一次上游（写入 ./data/radar.db）
python -m app.cli collect

# 起网页 → http://localhost:8000
uvicorn app.web:app --port 8000

# 跑测试
pytest -q
```

## 主要接口

| 接口 | 说明 |
|---|---|
| `GET /` | 中文检索页面 |
| `GET /changes` | 上游变更历史（原变更雷达视图） |
| `GET /api/search?q=&cat=&tags=` | **核心检索**：中文需求 → 精选 + 收录结果 |
| `GET /api/categories` | 精选分类及条目数 |
| `GET /api/curated` | 全部精选条目 |
| `GET /api/tags` | 标签释义（页面上给用户解释「需实名」是什么意思） |
| `GET /api/changes` · `/api/snapshots` · `/api/stats` | 变更 / 快照 / 统计 |
| `GET /metrics` | Prometheus 指标 · `GET /healthz` 健康检查 |

## 结构

```
app/
  data_curated.py   ← 人工精选数据（中文额度/要求/注意事项）★ 核心资产
  search.py         ← 中文检索层：同义词表、分类映射、标签换算
  web.py            ← FastAPI 路由
  static/index.html ← 中文检索页
  static/changes.html ← 变更历史页
  collector.py      ← 抓取 + 变更检测
  db.py / metrics.py / config.py / cli.py
k8s/                ← Deployment / CronJob / Ingress / NetworkPolicy（Argo CD 同步）
observability/      ← Prometheus / Grafana / Loki 配置
ops/                ← 集群运维手册、etcd 备份脚本
docs/postmortems/   ← 故障演练与事故报告
tests/              ← 45 个测试
```

## 贡献精选条目

新增一条只要往 `app/data_curated.py` 的 `ENTRIES` 里加一个 dict：

```python
{
    "id": "unique-slug",
    "name": "服务名",
    "category": "ai-api",            # 必须是 CATEGORIES 里声明过的
    "summary": "一句话说清它能干嘛",
    "quota": "免费额度的具体数字",
    "requirements": ["免信用卡"],     # 或 ["需实名", "免信用卡"]
    "china": "cn_ok",                # cn_ok / cn_partial / cn_no
    "expiry": "长期",
    "url": "https://...",
    "tips": "最容易踩的坑",
    "tags": "中文+英文检索关键词",
    "reviewed": "2026-09",
}
```

测试会校验字段完整性、id 唯一性、分类是否已声明，以及**标签换算是否正确**
（数据里写中文、筛选要用英文 key，这层映射漏了会导致筛选查出 0 条 —— 踩过）。
