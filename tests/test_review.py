"""复核流程的测试。

锁住的核心语义：**两个真相来源取较新的那个**。
- 代码里的 `reviewed`（数据内容的版本）
- 数据库里的 `confirmed_at`（用户运行时点了「还准」）

只要用户定期点一下确认，就不该因为"没改代码"而被判成过期。
"""

from __future__ import annotations

from datetime import date

import pytest

from app import data_curated, db, review


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 db.connect 指到临时库 —— 别碰真实数据。"""
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda *a, **k: real(tmp_path / "review.db"))
    return tmp_path


def test_confirm_marks_entry_as_fresh(sandbox):
    """确认后，即使 reviewed 很旧，也不该再算待复核。"""
    eid = data_curated.as_list()[0]["id"]
    review.confirm(eid)

    st = review.statuses()
    assert st[eid]["needs_review"] is False
    assert st[eid]["confirmed_at"] is not None
    # 生效日期取的是「确认时间」而不是代码里那个旧月份
    today = date.today()
    assert st[eid]["effective"] == "%04d-%02d" % (today.year, today.month)


def test_confirm_is_idempotent(sandbox):
    eid = data_curated.as_list()[0]["id"]
    a = review.confirm(eid)
    b = review.confirm(eid)
    assert a["entry_id"] == b["entry_id"]
    assert len(review.confirmations()) == 1


def test_forget_removes_confirmation(sandbox):
    eid = data_curated.as_list()[0]["id"]
    review.confirm(eid)
    assert review.forget(eid) is True
    assert eid not in review.confirmations()
    # 第二次删就没东西可删了
    assert review.forget(eid) is False


def test_confirm_rejects_unknown_entry(sandbox):
    """只允许确认真实存在的精选条目，否则库里会攒一堆对不上的孤儿记录。"""
    with pytest.raises(ValueError):
        review.confirm("这条根本不存在")


def test_checkpoints_are_concrete():
    """复核要点必须具体到能照着做 —— 否则点开官网也不知道要对什么。"""
    entry = {
        "quota": "100 万 tokens",
        "expiry": "90 天",
        "requirements": ["需实名", "免信用卡"],
        "china": "cn_ok",
    }
    pts = review.checkpoints(entry)
    joined = " ".join(pts)
    assert "100 万 tokens" in joined, "要指出额度的当前记录值"
    assert "90 天" in joined
    assert "免信用卡" in joined
    assert "实名" in joined
    assert "直连" in joined
    assert len(pts) >= 5


def test_checkpoints_handles_missing_fields():
    """字段缺失不能让复核页崩掉。"""
    pts = review.checkpoints({})
    assert isinstance(pts, list) and pts, "至少要有兜底的一条"


def test_checklist_sorts_never_reviewed_first(sandbox):
    """从没核对过的排最前 —— 它们风险最高。"""
    items = data_curated.as_list()
    review.confirm(items[0]["id"])          # 确认一条，它就不该出现在清单里
    cl = review.checklist()
    assert items[0]["id"] not in {i["id"] for i in cl["items"]}
    # 清单里每一条都带复核要点和官网链接字段
    for it in cl["items"]:
        assert it["checkpoints"]
        assert "url" in it


def test_checklist_counts_match(sandbox):
    cl = review.checklist()
    assert cl["total"] == len(data_curated.as_list())
    assert cl["pending"] == len(cl["items"])
    assert cl["interval_months"] == data_curated.REVIEW_INTERVAL_MONTHS


def test_confirmed_at_wins_over_older_reviewed():
    """纯函数层面的合并语义（不碰数据库）。"""
    # 数据标注是 2026-01（早就该复核），但用户在 2026-10 确认过
    st = data_curated.review_status("2026-01", "2026-10-02T10:00:00+00:00",
                                    today=date(2026, 10, 2))
    assert st["effective"] == "2026-10"
    assert st["needs_review"] is False

    # 反过来：数据在 2026-09 更新过，用户确认是更早的 2026-05 → 取 2026-09
    st2 = data_curated.review_status("2026-09", "2026-05-01T10:00:00+00:00",
                                     today=date(2026, 10, 2))
    assert st2["effective"] == "2026-09"


def test_broken_confirmed_at_is_ignored():
    """确认时间格式坏掉时，退回到 reviewed —— 不能让整条变成"从未核对"。"""
    st = data_curated.review_status("2026-09", "乱七八糟", today=date(2026, 10, 2))
    assert st["effective"] == "2026-09"
    assert st["needs_review"] is False
