"""README 解析器测试。"""

from __future__ import annotations

from pathlib import Path

from app.parser import parse

FIXTURE = (Path(__file__).parent / "fixtures" / "sample.md").read_text(encoding="utf-8")


def _by_name(entries, name):
    return next(e for e in entries if e.name == name)


def test_skips_table_of_contents():
    """目录里的锚点链接不能混进结果。"""
    entries = parse(FIXTURE)
    # 目录条目（锚点链接）必须全部被丢掉
    assert not [e for e in entries if (e.url or "").startswith("#")]
    # 目录里出现的分类名不能变成条目 —— 它只是分类，不是一条服务
    assert not [e for e in entries if e.name == "Major Cloud Providers"]
    # 真正的条目还在，并且归属正确的分类
    assert _by_name(entries, "Example Cloud").category == "Major Cloud Providers"


def test_parses_categories():
    categories = {e.category for e in parse(FIXTURE)}
    assert categories == {"Major Cloud Providers", "Email", "Other Free Resources"}


def test_nested_entries_carry_parent():
    entries = parse(FIXTURE)
    compute = _by_name(entries, "Compute")
    assert compute.parent == "Example Cloud"
    assert compute.url is None
    assert compute.description == "1 free micro instance per month"


def test_nested_entry_keeps_link():
    storage = _by_name(parse(FIXTURE), "Object Storage")
    assert storage.parent == "Example Cloud"
    assert storage.url == "https://example.com/storage"
    assert storage.description == "5GB free, 1GB egress"


def test_entry_without_link():
    plain = _by_name(parse(FIXTURE), "Plain Service")
    assert plain.url is None
    assert plain.parent is None
    assert plain.description == "10 requests per month, no link at all"


def test_key_is_unique_and_path_shaped():
    entries = parse(FIXTURE)
    keys = [e.key for e in entries]
    assert len(keys) == len(set(keys))
    assert _by_name(entries, "Compute").key == "Major Cloud Providers||Example Cloud > Compute"
    assert _by_name(entries, "Mailer").key == "Email||Mailer"


def test_same_entry_in_same_category_has_stable_key():
    """顺序变了但内容没变，key 必须一致，否则每次采集都会误报。"""
    first = [e.key for e in parse(FIXTURE)]
    second = [e.key for e in parse(FIXTURE)]
    assert first == second


def test_empty_input_returns_nothing():
    assert parse("") == []


# --- 回归：行内链接不在行首 ---
# 上游真实存在 `* Instances will be reclaimed when [deemed idle](url)` 这种写法。
# 早期版本把整串原始 markdown 当成了条目名，导致 URL 一变就误报「改名」。

INLINE_LINK_MARKDOWN = """\
## Major Cloud Providers

  * [Oracle Cloud](https://oracle.com)
    * Instances will be reclaimed when [deemed idle](https://docs.oracle.com/a/b#c) is true
"""


def test_inline_link_in_name_is_normalized():
    entry = _by_name(parse(INLINE_LINK_MARKDOWN), "Instances will be reclaimed when deemed idle is true")
    assert entry.parent == "Oracle Cloud"
    assert "https://docs.oracle.com" not in entry.name
    assert "[deemed idle]" not in entry.name


def test_inline_link_change_does_not_look_like_a_rename():
    """锚点变了、文案没变 → 条目 key 必须保持一致。"""
    before = parse(INLINE_LINK_MARKDOWN)
    after = parse(INLINE_LINK_MARKDOWN.replace("https://docs.oracle.com/a/b#c", "https://docs.oracle.com/x/y#z"))
    assert [e.key for e in before] == [e.key for e in after]


def test_bold_and_code_markers_are_stripped():
    markdown = "## Cat\n\n  * [Svc](https://s.example) - **Note**: `1 GB` free\n"
    entry = parse(markdown)[0]
    assert entry.description == "Note: 1 GB free"


# --- 回归：上游混用破折号做分隔符 ---
# 部分条目写成 `* [Name](url) — 描述`，早期只认半角 ` - `，
# 结果整行原始文本被当成了条目名。

DASH_MARKDOWN = """\
## Others

  * [Metashot](https://metashot.example) — Open Graph preview image API
  * Plain Thing – free for one project
  * x86-64 Service - 2 vCPU, e2-micro grade
"""


def test_em_dash_separator():
    entry = _by_name(parse(DASH_MARKDOWN), "Metashot")
    assert entry.url == "https://metashot.example"
    assert entry.description == "Open Graph preview image API"


def test_en_dash_separator():
    entry = _by_name(parse(DASH_MARKDOWN), "Plain Thing")
    assert entry.description == "free for one project"


def test_hyphen_inside_a_word_is_not_a_separator():
    """x86-64 / e2-micro 里的连字符两侧没空格，不能被切开。"""
    entry = _by_name(parse(DASH_MARKDOWN), "x86-64 Service")
    assert entry.description == "2 vCPU, e2-micro grade"
