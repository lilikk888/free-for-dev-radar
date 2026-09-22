"""中文检索层的测试。

这些用例覆盖了三类真实踩过的坑：
1. 标签筛选对不上（数据写中文、筛选用英文 key）→ 曾查出 0 条
2. 「免信用卡的」这种纯属性查询被当成正文去匹配 → 曾查出 0 条
3. 精选数据的字段完整性（少字段页面上会显示空白）
"""

from __future__ import annotations

from app import data_curated, search

REQUIRED_FIELDS = ("id", "name", "category", "summary", "quota", "url", "tags", "reviewed")


# ── 数据完整性 ────────────────────────────────────────────────────────────
def test_curated_entries_have_required_fields():
    for e in data_curated.as_list():
        for f in ("id", "name", "category", "summary", "url"):
            assert e.get(f), f"条目 {e.get('id')} 缺少字段 {f}"


def test_curated_ids_are_unique():
    ids = [e["id"] for e in data_curated.as_list()]
    assert len(ids) == len(set(ids)), "存在重复的 id"


def test_all_categories_are_declared():
    declared = {k for k, _ in data_curated.CATEGORIES}
    used = {e["category"] for e in data_curated.as_list()}
    assert used <= declared, f"有分类没在 CATEGORIES 里声明: {used - declared}"


# ── 标签换算 ──────────────────────────────────────────────────────────────
def test_entry_tags_maps_chinese_requirements_to_keys():
    entries = {e["id"]: e for e in data_curated.as_list()}
    tags = search.entry_tags(entries["zhipu"])
    assert "need_id" in tags, "「需实名」应换算成 need_id"
    assert "no_card" in tags, "「免信用卡」应换算成 no_card"


def test_entry_tags_includes_china_and_expiry():
    entries = {e["id"]: e for e in data_curated.as_list()}
    tags = search.entry_tags(entries["aliyun-bailian"])
    assert "cn_ok" in tags
    entries["oracle-always-free"]
    assert "permanent" in search.entry_tags(entries["oracle-always-free"])


# ── 属性词 → 筛选条件 ─────────────────────────────────────────────────────
def test_auto_tags_detects_natural_language():
    assert search.auto_tags("免信用卡的") == ["no_card"]
    assert "cn_ok" in search.auto_tags("国内能用的")
    assert "permanent" in search.auto_tags("永久免费的")


def test_residual_query_strips_attribute_words():
    # 「免信用卡的」剥完应该没有实质内容
    assert len(search.residual_query("免信用卡的")) < 2
    # 但「免信用卡的图床」应留下「图床」
    assert "图床" in search.residual_query("免信用卡的图床")


def test_pure_attribute_query_returns_results():
    """回归测试：曾经因为没剥属性词，这个查询返回 0 条。"""
    r = search.search("免信用卡的", foreign_rows=[], limit=99)
    assert r["counts"]["curated"] > 0
    assert "no_card" in r["applied_tags"]


# ── 检索质量 ──────────────────────────────────────────────────────────────
def test_chinese_query_hits_relevant_entries():
    r = search.search("免费的大模型 API", foreign_rows=[], limit=99)
    assert r["counts"]["curated"] > 0
    assert "ai-api" in r["guessed_categories"]
    names = [e["name"] for e in r["curated"][:10]]
    assert any("智谱" in n or "Gemini" in n or "百炼" in n for n in names)


def test_english_query_also_works():
    r = search.search("gpu", foreign_rows=[], limit=99)
    assert r["counts"]["curated"] > 0


def test_tag_filter_only_returns_matching_entries():
    r = search.search("", tags=["no_card"], foreign_rows=[], limit=999)
    assert r["counts"]["curated"] > 0
    for e in r["curated"]:
        assert "no_card" in e["tags"]


def test_category_filter():
    r = search.search("", cat="ai-api", foreign_rows=[], limit=999)
    assert r["counts"]["curated"] > 0
    for e in r["curated"]:
        assert e["category"] == "ai-api"


def test_foreign_entries_are_searched_too():
    rows = [
        {"entry_key": "k1", "category": "Generative AI", "name": "SomeAI",
         "url": "https://x", "description": "free AI api with monthly quota"},
    ]
    r = search.search("大模型", foreign_rows=rows, limit=10)
    assert r["counts"]["foreign"] >= 1
    assert r["foreign"][0]["category_cn"] == "生成式 AI"


def test_curated_results_carry_tags_for_ui():
    r = search.search("图床", foreign_rows=[], limit=10)
    for e in r["curated"]:
        assert isinstance(e.get("tags"), list) and e["tags"], "UI 依赖 tags 字段渲染标签"


def test_chinese_query_without_spaces():
    """回归测试：中文查询没有空格。

    「学生免费」这种连写（用户不会打空格）曾经查出 0 条 ——
    因为它既不在英文正文里，也不作为整串出现在标签中。
    解法是给中文词建自同义词。
    """
    for q in ["学生免费", "学生", "羊毛", "白嫖", "教育优惠"]:
        r = search.search(q, foreign_rows=[], limit=99)
        assert r["counts"]["curated"] > 0, f"「{q}」应该能搜到东西"


def test_student_category_is_guessed():
    r = search.search("学生免费", foreign_rows=[], limit=99)
    assert "student" in r["guessed_categories"]
    names = [e["name"] for e in r["curated"]]
    assert any("学生" in n for n in names), "学生专属福利应该在结果里"
