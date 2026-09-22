"""链接巡检的判定逻辑测试。

这里锁住的是**一条实测踩出来的规则**：
「站点还在」和「能自动打开」是两件事。

第一次跑真实巡检（在东京的集群里跑 88 条链接）时，把这些站点误判成"挂了"：
  - Mistral AI     → HTTP 307（重定向）
  - 华为云          → HTTP 418（反爬的玩笑状态码）
它们对用户都是能正常访问的，误报会让用户不再相信这个标记。
"""

from __future__ import annotations

from app import linkcheck


def test_2xx_is_reachable():
    assert linkcheck._classify(200) is True


def test_redirect_is_reachable():
    """回归测试：307 曾被判成挂了。"""
    for code in (301, 302, 303, 307, 308):
        assert linkcheck._classify(code) is True, f"HTTP {code} 是重定向，不该判成挂了"


def test_bot_refusal_is_reachable():
    """回归测试：418 曾被判成挂了。"""
    for code in (401, 403, 405, 418, 429):
        assert linkcheck._classify(code) is True, f"HTTP {code} 说明站点在，只是拒绝自动访问"


def test_gone_is_dead():
    for code in (404, 410, 451):
        assert linkcheck._classify(code) is False


def test_server_error_is_dead():
    for code in (500, 502, 503):
        assert linkcheck._classify(code) is False


def test_all_curated_urls_are_https():
    """精选数据里的链接必须都是 https —— 收录原则里 TLS 是底线。"""
    from app import data_curated

    bad = [e["id"] for e in data_curated.as_list() if not (e.get("url") or "").startswith("https://")]
    assert bad == [], f"这些条目的链接不是 https: {bad}"
