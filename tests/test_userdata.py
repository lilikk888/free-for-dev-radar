"""「我的领用记录」与到期提醒的测试。

重点锁住两个真实踩过的 bug：
1. 日期校验太松：`20260901`（无分隔符）曾被接受 → 库里混进两种格式，
   字符串比较排序失效。Python 3.11+ 的 `date.fromisoformat` 确实接受这种写法，
   所以必须额外用正则卡格式。
2. 清单里只有 entry_id 没有服务名 → 界面/邮件里显示「zhipu」用户看不懂。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import config, userdata


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """每个用例一个独立库，互不干扰。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_upsert_and_get(temp_db):
    item = userdata.upsert("zhipu", status="registered", expires_at="2026-12-31")
    assert item["entry_id"] == "zhipu"
    assert item["status"] == "registered"
    assert item["registered_at"] is not None
    assert userdata.get("zhipu")["expires_at"] == "2026-12-31"


def test_rejects_malformed_date(temp_db):
    """回归测试：无分隔符的日期必须被拒绝。"""
    with pytest.raises(ValueError):
        userdata.upsert("zhipu", status="registered", expires_at="20260901")


def test_rejects_impossible_date(temp_db):
    with pytest.raises(ValueError):
        userdata.upsert("zhipu", status="registered", expires_at="2026-02-30")


def test_rejects_unknown_status(temp_db):
    with pytest.raises(ValueError):
        userdata.upsert("zhipu", status="flying")


def test_days_left(temp_db):
    future = (date.today() + timedelta(days=5)).isoformat()
    assert userdata.days_left(future) == 5
    assert userdata.days_left("2020-01-01") < 0
    assert userdata.days_left(None) is None


def test_list_all_enriches_with_service_name(temp_db):
    """回归测试：清单里必须带服务名，不能只有 ID。"""
    userdata.upsert("zhipu", status="registered", expires_at="2026-12-31")
    rows = userdata.list_all()
    assert rows[0]["name"] == "智谱 AI（GLM）"
    assert rows[0]["url"].startswith("http")
    assert rows[0]["category_cn"]
    assert rows[0]["known"] is True


def test_unknown_entry_id_still_listed(temp_db):
    """精选数据里删掉的条目，历史记录不能凭空消失。"""
    userdata.upsert("some-removed-entry", status="registered")
    rows = userdata.list_all()
    assert rows[0]["entry_id"] == "some-removed-entry"
    assert rows[0]["known"] is False


def test_expiring_only_counts_registered(temp_db):
    soon = (date.today() + timedelta(days=3)).isoformat()
    userdata.upsert("zhipu", status="interested", expires_at=soon)      # 没注册 → 不该提醒
    userdata.upsert("aliyun-bailian", status="registered", expires_at=soon)
    items = userdata.expiring(within_days=14)
    assert [i["entry_id"] for i in items] == ["aliyun-bailian"]


def test_expiring_excludes_far_future(temp_db):
    far = (date.today() + timedelta(days=90)).isoformat()
    userdata.upsert("zhipu", status="registered", expires_at=far)
    assert userdata.expiring(within_days=14) == []


def test_upsert_is_idempotent_and_keeps_registered_at(temp_db):
    first = userdata.upsert("zhipu", status="registered", expires_at="2026-12-31")
    second = userdata.upsert("zhipu", status="registered", expires_at="2027-01-31")
    assert second["registered_at"] == first["registered_at"]
    assert second["expires_at"] == "2027-01-31"


def test_delete(temp_db):
    userdata.upsert("zhipu", status="registered")
    assert userdata.delete("zhipu") is True
    assert userdata.delete("zhipu") is False
    assert userdata.list_all() == []


def test_stats(temp_db):
    soon = (date.today() + timedelta(days=3)).isoformat()
    userdata.upsert("zhipu", status="registered", expires_at=soon)
    userdata.upsert("neon", status="interested")
    userdata.upsert("turso", status="dropped")
    st = userdata.stats()
    assert st["total"] == 3
    assert st["registered"] == 1
    assert st["interested"] == 1
    assert st["dropped"] == 1
    assert st["expiring_14d"] == 1
