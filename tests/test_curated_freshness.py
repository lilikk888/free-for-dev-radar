"""精选数据的「新鲜度」逻辑测试。

背景：精选这层数据是**人工维护**的，没人复核就会慢慢腐烂 ——
自动抓的那层有采集任务盯着、链接挂了有巡检盯着，
唯独「免费额度从 2000 万缩成 1000 万」没有任何机制能自动发现。

所以这里的做法不是假装能自动检测政策变化，而是**把新鲜度标出来**，
让「该复核了」这件事变得可见。测试锁住这个语义。
"""

from __future__ import annotations

from datetime import date

from app import data_curated


def test_recent_review_is_not_stale():
    # 2026-09 核对，到 2026-10 只过了 1 个月
    st = data_curated.review_status("2026-09", today=date(2026, 10, 2))
    assert st["review_age_months"] == 1
    assert st["needs_review"] is False


def test_exactly_at_threshold_is_stale():
    """刚好到 3 个月就该复核了（阈值是 >=，不是 >）。"""
    st = data_curated.review_status("2026-09", today=date(2026, 12, 1))
    assert st["review_age_months"] == 3
    assert st["needs_review"] is True


def test_long_overdue():
    st = data_curated.review_status("2025-01", today=date(2026, 10, 2))
    assert st["review_age_months"] == 21
    assert st["needs_review"] is True


def test_missing_field_treated_as_needs_review():
    """没写 reviewed 的条目应当被当成「待复核」—— 宁可提示，也不要静默过期。"""
    for bad in (None, "", "乱七八糟", "2026"):
        st = data_curated.review_status(bad)
        assert st["needs_review"] is True, f"{bad!r} 应该被判为待复核"
        assert st["review_age_months"] is None


def test_every_curated_entry_has_reviewed_field():
    """所有精选条目都必须带 reviewed —— 漏一个就会静默失去新鲜度追踪。"""
    missing = [e["id"] for e in data_curated.as_list() if not e.get("reviewed")]
    assert missing == [], f"这些条目缺 reviewed 字段: {missing}"


def test_review_summary_counts_stale_entries():
    items = [
        {"reviewed": "2026-09"},
        {"reviewed": "2025-01"},   # 早就该复核
        {"reviewed": None},        # 没有字段
    ]
    s = data_curated.review_summary(items, today=date(2026, 10, 2))
    assert s["total"] == 3
    assert s["needs_review"] == 2
    assert s["oldest_months"] == 21


def test_real_data_is_currently_within_interval():
    """整份数据的现状检查 —— 如果失败说明该去复核精选数据了（这是有意的提醒）。"""
    s = data_curated.review_summary()
    assert s["oldest_months"] is not None
    assert s["oldest_months"] < data_curated.REVIEW_INTERVAL_MONTHS, (
        f"精选数据已经 {s['oldest_months']} 个月没复核了，该更新 reviewed 字段了"
    )
