"""每周订阅邮件：把「本周新收录 / 即将到期 / 链接挂了」汇总成一封发给自己。

为什么用 SMTP 而不是像到期提醒那样走告警链路：
告警通道适合「出事了通知你」，而这是一封**内容邮件**（有列表、有链接、有排版），
塞进 Alertmanager 的模板里会很别扭。所以这里老实配 SMTP ——
但复用了同一组 163 凭据（从 Alertmanager 的配置里抄一份到独立 Secret，
应用只拿到自己需要的那几个变量）。

设计取舍：
- **没配 SMTP 就只打印不报错** —— 本地开发和单测不该依赖外部服务
- 邮件正文用内联样式的 HTML：邮件客户端对 <style> 支持很差，只能内联
- 没有新内容时**不发空邮件**（避免变成每周噪音，最后被自己拉黑）
"""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Any

from . import data_curated, linkcheck, userdata

SMTP_HOST = os.getenv("RADAR_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("RADAR_SMTP_PORT", "465"))
SMTP_USER = os.getenv("RADAR_SMTP_USER", "")
SMTP_PASS = os.getenv("RADAR_SMTP_PASS", "")
SMTP_FROM = os.getenv("RADAR_SMTP_FROM", "")
DIGEST_TO = os.getenv("RADAR_DIGEST_TO", "")
SITE_URL = os.getenv("RADAR_SITE_URL", "")


def _esc(s: Any) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _row(title: str, sub: str, url: str | None = None, tail: str = "") -> str:
    link = f'<a href="{_esc(url)}" style="color:#2563eb;text-decoration:none">{_esc(title)}</a>' if url else _esc(title)
    return (
        f'<div style="padding:9px 0;border-top:1px solid #e8eaed">'
        f'<div style="font-weight:600;font-size:14px">{link} {tail}</div>'
        f'<div style="color:#6b7280;font-size:13px;margin-top:2px">{_esc(sub)}</div>'
        f"</div>"
    )


def build() -> tuple[str, str, dict[str, int]]:
    """生成 (主题, HTML 正文, 统计)。"""
    fresh = linkcheck.newly_seen(since_days=7)
    due = userdata.expiring(within_days=14)
    dead = [v for v in linkcheck.latest().values() if not v["ok"]]
    index = {e["id"]: e for e in data_curated.as_list()}

    today = date.today().isoformat()
    subject = f"免费资源雷达周报 · {today}"
    if fresh:
        subject += f" · 新收录 {len(fresh)} 条"

    parts: list[str] = []
    parts.append(
        '<div style="font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        'max-width:640px;margin:0 auto;color:#1f2329;font-size:14px;line-height:1.7">'
        f'<h2 style="font-size:18px;margin:0 0 4px">免费资源雷达 · 周报</h2>'
        f'<div style="color:#8a9099;font-size:12.5px">{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>'
    )

    if fresh:
        parts.append(f'<h3 style="font-size:15px;margin:22px 0 6px">本周新收录（{len(fresh)} 条）</h3>')
        for e in fresh:
            parts.append(_row(e["name"], (e.get("summary") or "")[:110], e.get("url")))
    else:
        parts.append('<h3 style="font-size:15px;margin:22px 0 6px">本周新收录</h3>'
                     '<div style="color:#6b7280;font-size:13px">无（这周没有新整理进来的条目）</div>')

    if due:
        parts.append(f'<h3 style="font-size:15px;margin:22px 0 6px">额度快到期了（{len(due)} 条）</h3>')
        for it in due:
            d = it["days_left"]
            tail = f'<span style="color:#d93838">还剩 {d} 天</span>' if d >= 0 else f'<span style="color:#d93838">已过期 {-d} 天</span>'
            parts.append(_row(it["name"], it.get("quota") or "", it.get("url"), tail))
    else:
        parts.append('<h3 style="font-size:15px;margin:22px 0 6px">额度到期</h3>'
                     '<div style="color:#6b7280;font-size:13px">14 天内没有要过期的额度</div>')

    if dead:
        parts.append(f'<h3 style="font-size:15px;margin:22px 0 6px">链接可能挂了（{len(dead)} 条）</h3>')
        for v in dead:
            e = index.get(v["entry_id"], {})
            parts.append(_row(e.get("name", v["entry_id"]), v.get("error") or "无法访问", e.get("url")))
    else:
        parts.append('<h3 style="font-size:15px;margin:22px 0 6px">链接巡检</h3>'
                     '<div style="color:#6b7280;font-size:13px">全部正常</div>')

    if SITE_URL:
        parts.append(
            f'<div style="margin-top:24px"><a href="{_esc(SITE_URL)}" '
            f'style="background:#2563eb;color:#fff;padding:9px 16px;border-radius:8px;'
            f'text-decoration:none;font-size:13.5px">打开免费资源雷达</a></div>'
        )
    parts.append('<div style="margin-top:20px;color:#9aa1a9;font-size:12px">'
                 '免费政策随时会变，注册前请以官网为准。</div></div>')

    return subject, "".join(parts), {"fresh": len(fresh), "due": len(due), "dead": len(dead)}


def send(subject: str, html: str) -> tuple[bool, str]:
    """发信。没配 SMTP 时只打印，不报错（本地/单测友好）。"""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return False, "未配置 SMTP，跳过发信（正文见上方输出）"
    to = DIGEST_TO or SMTP_USER
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = to
    msg.set_content("这是一封 HTML 邮件，请用支持 HTML 的客户端查看。")
    msg.add_alternative(html, subtype="html")

    try:
        # 163 的 465 是隐式 TLS；用 SMTP_SSL，不要再叠加 STARTTLS
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True, f"已发送到 {to}"
    except Exception as exc:  # pragma: no cover - 网络路径
        return False, f"发送失败: {type(exc).__name__}: {exc}"
