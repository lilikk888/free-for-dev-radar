"""把 free-for-dev 的 README 解析成结构化条目。

README 实测格式（来自 master 分支）：

    # free-for.dev
    ...
    # Table of Contents
      * [Major Cloud Providers](#major-cloud-providers)     <- 目录，必须跳过
    ...
    ## Major Cloud Providers                                <- 二级标题即分类
      * [Google Cloud Platform](https://cloud.google.com)   <- 2 空格缩进 = 顶级条目
        * Compute Engine - 1 e2-micro ...                   <- 4 空格缩进 = 子条目
        * [Cloud Run](https://...) - 2M requests ...

要点：
1. `# Table of Contents` 到第一个 `##` 之间全是目录，跳过。
2. 条目不一定带 markdown 链接，也可能是 `  * 纯文本 - 描述`。
3. 免费额度写在子条目的描述里，所以子条目必须单独解析，不能只取顶级。
4. 描述里可能还有别的 markdown 链接，解析时只认行首那一个。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORY_RE = re.compile(r"^##\s+(?P<name>\S.*?)\s*$")
LIST_RE = re.compile(r"^(?P<indent> *)[*+-]\s+(?P<body>\S.*?)\s*$")
LINK_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\((?P<url>[^)]+)\)(?:\s+[-–—]\s+(?P<desc>.*))?$")

# 行内 markdown：上游存在 `* 文本 [链接](url)` 这种链接不在行首的写法
INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
CODE_RE = re.compile(r"`(.+?)`")

# 上游混用半角连字符和全角破折号做分隔符，三种都要认。
# 用「两侧必须有空格」来避免误切 x86-64 / e2-micro 这类连字符。
DESCRIPTION_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")

TOC_MARKER = "table of contents"


@dataclass(frozen=True)
class Entry:
    """一个可被追踪的原子条目。"""

    category: str
    parent: str | None
    name: str
    url: str | None
    description: str

    @property
    def key(self) -> str:
        """稳定标识：分类 + 完整路径。parent 变了就视为另一个条目。"""
        path = f"{self.parent} > {self.name}" if self.parent else self.name
        return f"{self.category}||{path}"

    @property
    def display_path(self) -> str:
        if self.parent:
            return f"{self.category} > {self.parent} > {self.name}"
        return f"{self.category} > {self.name}"


def _normalize(text: str) -> str:
    """把行内 markdown 折成纯文本，并压掉多余空白。

    必要性：上游有 `* Instances will be reclaimed when [deemed idle](url) is true`
    这种写法，链接不在行首。如果不折掉，URL 一变就会被误判成「条目改名」。
    """
    text = INLINE_LINK_RE.sub(r"\1", text)
    text = BOLD_RE.sub(r"\1", text)
    text = CODE_RE.sub(r"\1", text)
    return " ".join(text.split())


def _parse_body(body: str) -> tuple[str, str | None, str]:
    """拆出 (名称, 链接, 描述)。"""
    link = LINK_RE.match(body)
    if link:
        return (
            _normalize(link.group("name")),
            link.group("url").strip(),
            _normalize(link.group("desc") or ""),
        )

    parts = DESCRIPTION_SEPARATOR_RE.split(_normalize(body), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), None, parts[1].strip()
    return _normalize(body), None, ""


def parse(markdown: str) -> list[Entry]:
    """解析整份 README。返回的条目按文档出现顺序排列。"""
    entries: list[Entry] = []
    category: str | None = None
    parent: str | None = None
    in_toc = False

    for raw in markdown.splitlines():
        heading = CATEGORY_RE.match(raw)
        if heading:
            # 碰到第一个真正的分类，说明目录结束
            in_toc = False
            category = heading.group("name")
            parent = None
            continue

        if raw.startswith("# "):
            # 一级标题：只有 Table of Contents 之后的列表需要跳过
            in_toc = TOC_MARKER in raw.lower()
            continue

        if in_toc or category is None:
            continue

        item = LIST_RE.match(raw)
        if item is None:
            continue

        name, url, description = _parse_body(item.group("body"))
        if not name:
            continue
        # 目录条目、锚点链接：丢掉
        if url and url.startswith("#"):
            continue

        if len(item.group("indent")) <= 2:
            parent = name
            entries.append(
                Entry(category=category, parent=None, name=name, url=url, description=description)
            )
        else:
            entries.append(
                Entry(category=category, parent=parent, name=name, url=url, description=description)
            )

    return entries
