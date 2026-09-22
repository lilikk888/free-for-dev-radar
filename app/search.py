"""中文检索层：把「我需要什么」这句中文，翻译成能命中数据的搜索。

数据有两层，职责不同：

| 层 | 来源 | 条目量 | 特点 |
|---|---|---|---|
| **精选** | `data_curated.py`（人工整理） | 60+ | 中文说明、额度、要求（要不要实名/绑卡）、国内可用性、坑。**国内平台为主** |
| **收录** | free-for-dev 抓取 | 1300+ | 覆盖海外服务，只有英文原文，靠中文分类 + 同义词表桥接 |

这样设计的原因：free-for-dev 里**一条国内平台都没有**，而国内免费额度（智谱 2000 万
tokens、百炼 100 万……）恰恰是中文用户最需要的。只做抓取等于把最有用的部分漏掉了。
"""

from __future__ import annotations

import re
from typing import Iterable

from . import data_curated

# ── free-for-dev 的英文章节名 → 中文分类名 ────────────────────────────────
# 没列到的章节会落到「其他」。
CATEGORY_CN: dict[str, str] = {
    "Major Cloud Providers": "云服务商（大厂）",
    "IaaS": "基础设施即服务",
    "PaaS": "平台即服务（部署）",
    "Web Hosting": "网站托管",
    "CDN and Protection": "CDN 与防护",
    "DNS": "DNS 解析",
    "Domain": "域名",
    "Managed Data Services": "托管数据库",
    "Data Management": "数据管理",
    "Database": "数据库",
    "Storage and Media Processing": "存储与媒体处理",
    "CI and CD": "CI / CD",
    "Source Code Repos": "代码仓库",
    "Code Quality": "代码质量",
    "Testing": "测试",
    "Monitoring": "监控",
    "Log Management": "日志管理",
    "Crash and Exception Handling": "崩溃与异常收集",
    "Status Pages": "状态页",
    "Analytics, Events and Statistics": "分析与统计",
    "Email": "邮件",
    "Messaging and Streaming": "消息与推送",
    "Search": "搜索服务",
    "Security and PKI": "安全与证书",
    "Identity and Access Management": "认证与权限",
    "Authentication": "认证登录",
    "Generative AI": "生成式 AI",
    "APIs, Data and ML": "API / 数据 / 机器学习",
    "Machine Learning": "机器学习",
    "Artifact Repos": "制品仓库",
    "BaaS": "后端即服务",
    "CMS": "内容管理系统",
    "Design and UI": "设计与 UI",
    "Font": "字体",
    "Forms": "表单",
    "Education": "学习资源",
    "IDE and Code Editing": "在线 IDE",
    "Issue Tracking and Project Management": "项目管理",
    "Team collaboration tools": "团队协作",
    "Payments and Billing": "支付与账单",
    "Privacy Management": "隐私管理",
    "Translation": "翻译",
    "Tunneling, WebRTC, Web Socket Servers and Other Routers": "内网穿透 / 隧道",
    "Miscellaneous": "杂项",
    "Management Systems": "管理系统",
    "Package Builders": "打包构建",
}

# ── 中文关键词 → 英文检索词 ────────────────────────────────────────────────
# 做中文搜索的关键：用户输入中文，数据是英文，必须靠这张表桥接。
SYNONYMS: dict[str, list[str]] = {
    "服务器": ["server", "instance", "compute", "vps", "iaas", "ec2", "vm"],
    "主机": ["server", "instance", "hosting", "vps", "vm"],
    "云": ["cloud", "iaas", "paas"],
    "数据库": ["database", "db", "sql", "postgres", "postgresql", "mysql", "mongodb", "redis"],
    "存数据": ["database", "storage"],
    "缓存": ["redis", "cache", "memcached"],
    "队列": ["queue", "kafka", "rabbitmq", "messaging", "streaming"],
    "存储": ["storage", "object", "bucket", "s3", "drive", "media"],
    "网盘": ["storage", "drive", "file"],
    "图床": ["image", "img", "photo", "picture", "upload", "media"],
    "图片": ["image", "img", "photo", "picture", "media", "cdn"],
    "视频": ["video", "media", "streaming"],
    "域名": ["domain", "subdomain", "dns", "nameserver"],
    "解析": ["dns", "nameserver", "resolve"],
    "证书": ["ssl", "tls", "certificate", "pki", "https"],
    "https": ["ssl", "tls", "certificate"],
    "加速": ["cdn", "protection", "edge", "cache"],
    "邮件": ["email", "mail", "smtp", "transactional"],
    "发邮件": ["email", "smtp", "mail", "transactional"],
    "短信": ["sms", "messaging", "notification"],
    "推送": ["push", "notification", "messaging", "streaming"],
    "监控": ["monitoring", "uptime", "alert", "apm", "observability"],
    "告警": ["alert", "alerting", "monitoring", "notification"],
    "报警": ["alert", "monitoring", "notification"],
    "日志": ["log", "logging", "logs"],
    "错误": ["error", "crash", "exception", "sentry"],
    "崩了": ["crash", "error", "exception"],
    "ai": ["ai", "ml", "machine learning", "llm", "gpt", "generative", "model", "inference"],
    "大模型": ["llm", "ai", "gpt", "generative", "model", "ai api"],
    "人工智能": ["ai", "ml", "machine learning"],
    "模型": ["model", "ai", "llm", "inference"],
    "接口": ["api", "endpoint", "rest"],
    "显卡": ["gpu", "compute", "cuda", "training"],
    "算力": ["gpu", "compute", "training", "notebook"],
    "训练": ["training", "gpu", "machine learning", "notebook"],
    "部署": ["deploy", "hosting", "paas", "static"],
    "托管": ["hosting", "paas", "static", "deploy"],
    "网站": ["website", "hosting", "static", "web"],
    "博客": ["blog", "static", "hosting", "pages"],
    "前端": ["static", "frontend", "cdn", "pages"],
    "后端": ["backend", "paas", "baas", "serverless", "function"],
    "无服务器": ["serverless", "function", "lambda", "workers", "edge"],
    "函数": ["function", "serverless", "lambda", "workers"],
    "流水线": ["ci", "cd", "pipeline", "build", "actions"],
    "持续集成": ["ci", "cd", "pipeline", "build"],
    "构建": ["build", "ci", "cd", "artifact"],
    "代码": ["code", "repo", "git", "source"],
    "代码托管": ["repo", "source code", "git", "gitlab", "github", "bitbucket"],
    "协作": ["team", "collaboration", "project management", "issue"],
    "认证": ["auth", "authentication", "identity", "sso", "login", "oauth"],
    "登录": ["auth", "login", "identity", "sso"],
    "用户": ["auth", "identity", "user management"],
    "支付": ["payment", "billing", "checkout"],
    "测试": ["testing", "test", "qa"],
    "表单": ["form", "forms", "survey"],
    "搜索": ["search", "index", "algolia"],
    "翻译": ["translation", "translate", "i18n"],
    "内网穿透": ["tunnel", "tunneling", "ngrok", "proxy", "router"],
    "穿透": ["tunnel", "tunneling", "proxy"],
    "验证码": ["captcha", "turnstile", "recaptcha", "security"],
    "学习": ["education", "course", "learning", "training"],
    "字体": ["font", "typography"],
    "设计": ["design", "ui", "icons", "assets"],
    "图标": ["icons", "design", "assets"],
    "状态页": ["status", "statuspage", "uptime"],
    "统计": ["analytics", "statistics", "events"],
    "身份": ["identity", "auth", "sso"],
    "备份": ["backup", "storage", "snapshot"],
    # ⭐ 这几个是「中文没有空格」暴露出来的问题：
    # 用户搜「学生免费」时，整串中文既不会命中英文正文，也不在标签里逐字出现，
    # 结果 0 条（本地测试里带了空格的「学生 免费」却能搜到）。
    # 解法：给中文词也建自同义词，让它能命中标签里的中文。
    "学生": ["学生", "student", "education", "校园", "高校", "教育"],
    "教育": ["教育", "学生", "education", "course"],
    "校园": ["校园", "学生", "高校", "education"],
    "高校": ["高校", "学生", "校园", "education"],
    "羊毛": ["羊毛", "白嫖", "免费", "赠送", "free", "credit"],
    "白嫖": ["白嫖", "羊毛", "免费", "赠送", "free", "credit"],
    "赠送": ["赠送", "免费", "credit", "trial", "free"],
    "额度": ["额度", "quota", "credit", "limit", "free"],
    "试用": ["试用", "trial", "free"],
    "开源": ["开源", "open source", "self-hosted"],
}

# ── 需求 → 推荐分类（首页「我想找…」按钮用，也给搜索结果做加权）────────
NEED_HINTS: list[tuple[str, list[str]]] = [
    ("student", ["学生", "校园", "高校", "教育", "student", "教育优惠"]),
    ("ai-api", ["ai", "大模型", "llm", "gpt", "模型", "接口", "api", "tokens", "token", "羊毛", "白嫖"]),
    ("ai-compute", ["算力", "gpu", "显卡", "训练", "炼丹", "notebook", "colab", "kaggle"]),
    ("cloud", ["服务器", "主机", "云服务", "vps", "免费服务器", "云主机", "instance"]),
    ("hosting", ["部署", "托管", "网站", "博客", "前端", "静态"]),
    ("database", ["数据库", "postgres", "mysql", "mongodb", "redis", "sql", "存数据"]),
    ("storage", ["存储", "图床", "网盘", "图片", "对象存储", "备份"]),
    ("cdn-dns", ["域名", "dns", "证书", "ssl", "https", "cdn", "加速", "穿透", "隧道"]),
    ("cicd", ["ci", "cd", "流水线", "持续集成", "构建", "代码托管", "git"]),
    ("monitor", ["监控", "告警", "报警", "日志", "错误", "可用性", "状态页"]),
    ("email", ["邮件", "发邮件", "smtp", "短信"]),
    ("auth", ["认证", "登录", "用户", "身份"]),
    ("devtool", ["验证码", "字体", "图标", "表单", "工具", "ide"]),
]

# ── 精选数据里的中文要求 → 筛选标签 key ──────────────────────────────────
# 数据里写中文是为了给人读（页面上直接显示），筛选要用稳定的英文 key，
# 这张表负责两边对上。**曾经漏了这层映射，导致「免信用卡」筛选查出 0 条。**
REQ_TO_TAG: dict[str, str] = {
    "免信用卡": "no_card",
    "无需信用卡": "no_card",
    "需信用卡": "need_card",
    "需绑卡": "need_card",
    "需信用卡验证（不扣费）": "need_card",
    "需实名": "need_id",
    "需实名认证": "need_id",
    "需手机验证": "need_id",
    "需实名审核": "need_id",
}

# ── 用户直接说需求时，自动转成筛选条件 ────────────────────────────────────
# 例如搜「免信用卡的」→ 应该变成「标签=免信用卡」而不是当成关键词去匹配文本
ATTR_HINTS: dict[str, list[str]] = {
    "no_card": ["免信用卡", "不绑卡", "不用绑卡", "不要信用卡", "无信用卡", "不用银行卡", "不绑银行卡"],
    "need_id": ["需实名", "要实名", "需要实名"],
    "cn_ok": ["国内可用", "国内直连", "国内能用", "不用梯子", "免梯子", "不用翻墙"],
    "permanent": ["长期免费", "永久免费", "永久可用"],
}


def auto_tags(query: str) -> list[str]:
    """从自然语言需求里推断筛选标签。"""
    q = query.strip().lower()
    return [tag for tag, words in ATTR_HINTS.items() if any(w in q for w in words)]


def entry_tags(entry: dict) -> list[str]:
    """把一条精选数据换算成标准标签集合。"""
    tags = set()
    for req in entry.get("requirements", []):
        if req in REQ_TO_TAG:
            tags.add(REQ_TO_TAG[req])
        elif req.startswith("免"):
            tags.add("no_card")
    china = entry.get("china", "")
    if china:
        tags.add(china)
    expiry = entry.get("expiry", "")
    if "永久" in expiry or "长期" in expiry:
        tags.add("permanent")
    elif expiry:
        tags.add("limited")
    return sorted(tags)

_CJK = re.compile(r"[\u4e00-\u9fff]")


def residual_query(query: str) -> str:
    """剥离属性词后剩下的「真正的检索内容」。

    用户搜「免信用卡的」时，整句其实是个筛选条件，不是要匹配的文本。
    不剥离的话会拿整句去比对正文 → 一个字都匹配不上 → 查出 0 条
    （这个 bug 实测复现过）。
    """
    q = query
    for words in ATTR_HINTS.values():
        for w in words:
            q = q.replace(w, " ")
    # 去掉「的」「有」「吗」这类没有检索意义的虚词
    for noise in ("的", "有没有", "有", "吗", "吧", "呢", "和", "与", " "):
        q = q.replace(noise, " ")
    return q.strip()


def _expand(query: str) -> tuple[str, list[str]]:
    """把用户输入拆成 (中文原文, 扩展出的英文检索词)。"""
    q = query.strip().lower()
    english: list[str] = []
    # 直接输入英文时也支持
    for word in re.findall(r"[a-z0-9]{2,}", q):
        english.append(word)
    for cn, words in SYNONYMS.items():
        if cn in q:
            english.extend(words)
    # 去重且保持顺序
    seen: set[str] = set()
    uniq = [w for w in english if not (w in seen or seen.add(w))]
    return q, uniq


def guess_categories(query: str) -> list[str]:
    """从中文需求里猜用户想找哪一类（用于结果排序加权）。"""
    q = query.strip().lower()
    hits: list[tuple[int, str]] = []
    for cat, words in NEED_HINTS:
        score = sum(1 for w in words if w in q)
        if score:
            hits.append((score, cat))
    hits.sort(reverse=True)
    return [c for _, c in hits]


def _score_curated(entry: dict, raw: str, english: Iterable[str], cats: list[str]) -> float:
    hay_name = entry.get("name", "").lower()
    hay_summary = entry.get("summary", "").lower()
    hay_quota = entry.get("quota", "").lower()
    hay_tags = entry.get("tags", "").lower()
    hay_tips = entry.get("tips", "").lower()
    cat_name = data_curated.category_name(entry.get("category", "")).lower()
    score = 0.0

    if raw and len(raw) >= 2:
        if raw in hay_name:
            score += 10
        if raw in hay_tags:
            score += 8
        if raw in hay_summary:
            score += 5
        if raw in hay_quota:
            score += 3
        if raw in hay_tips:
            score += 2
        if raw in cat_name:
            score += 4

    for token in english:
        if token in hay_tags:
            score += 3
        if token in hay_name:
            score += 2
        if token in hay_summary:
            score += 1.5
        if token in hay_quota:
            score += 1
        if token in cat_name:
            score += 2

    if entry.get("category") in cats:
        score += 3
    # 精选条目默认给一点基础分：它们信息更全、更新更勤
    if score > 0:
        score += 1.5
    return score


def _score_foreign(entry: dict, raw: str, english: Iterable[str], cats: list[str]) -> float:
    name = (entry.get("name") or "").lower()
    desc = (entry.get("description") or "").lower()
    cat = (entry.get("category") or "")
    cat_cn = CATEGORY_CN.get(cat, cat).lower()
    score = 0.0

    if raw and len(raw) >= 2:
        if raw in name:
            score += 8
        if raw in desc:
            score += 3
        if raw in cat_cn:
            score += 4

    for token in english:
        if token in name:
            score += 2
        if token in desc:
            score += 1
        if token in cat_cn:
            score += 1.5
    return score


def _curated_results(raw: str, english: list[str], cats: list[str],
                     tag_filter: list[str], cat_filter: str | None) -> list[dict]:
    out = []
    for e in data_curated.as_list():
        if cat_filter and e.get("category") != cat_filter:
            continue
        etags = entry_tags(e)
        if tag_filter and not all(t in etags for t in tag_filter):
            continue
        if raw:
            s = _score_curated(e, raw, english, cats)
            if s <= 0:
                continue
        else:
            s = 1.0
        out.append({**e, "tags": etags, "source": "curated", "score": round(s, 2)})
    return out


def _foreign_results(rows: list[dict], raw: str, english: list[str], cats: list[str],
                     cat_filter: str | None) -> list[dict]:
    out = []
    for r in rows:
        if cat_filter:
            continue  # 精选分类与英文章节不是同一套 key，筛选时只筛精选
        s = _score_foreign(r, raw, english, cats) if raw else 0.0
        if s <= 0:
            continue
        out.append({
            "id": "ffd:" + (r.get("entry_key") or ""),
            "name": r.get("name"),
            "category": r.get("category"),
            "category_cn": CATEGORY_CN.get(r.get("category"), r.get("category")),
            "summary": r.get("description") or "",
            "quota": "",
            "requirements": [],
            "china": "",
            "expiry": "",
            "url": r.get("url"),
            "tips": "来自 free-for-dev（英文原文，未人工核实）",
            "tags": "",
            "source": "free-for-dev",
            "score": round(s, 2),
        })
    return out


def search(
    query: str = "",
    cat: str | None = None,
    tags: list[str] | None = None,
    foreign_rows: list[dict] | None = None,
    limit: int = 60,
) -> dict:
    """统一检索入口。返回 {curated: [...], foreign: [...], categories: [...]}。"""
    # 用户可能把筛选条件直接说进需求里（「免信用卡的」「国内能用的」），
    # 这类词不该拿去匹配正文，而是应该变成筛选条件。
    inferred = auto_tags(query)
    merged_tags = list({*(tags or []), *inferred})

    # 关键：把属性词剥离掉再当检索文本。
    # 否则「免信用卡的」整句拿去匹配正文 → 0 条（实测踩过）。
    text = residual_query(query) if inferred else query
    if len(text) < 2:
        text = ""

    raw, english = _expand(text)
    cats = guess_categories(text or query)

    curated = _curated_results(raw, english, cats, merged_tags, cat)
    curated.sort(key=lambda x: (-x["score"], x["category"]))

    foreign: list[dict] = []
    if foreign_rows and raw:
        foreign = _foreign_results(foreign_rows, raw, english, cats, cat)
        foreign.sort(key=lambda x: -x["score"])

    return {
        "query": query,
        "guessed_categories": cats,
        "applied_tags": merged_tags,
        "curated": curated[:limit],
        "foreign": foreign[:limit],
        "counts": {"curated": len(curated), "foreign": len(foreign)},
    }
