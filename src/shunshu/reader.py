"""抓取 + 识别 + 分页合并，产出可用的章节列表。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Rules
from .http import Fetcher
from .models import Chapter, Toc
from .toc_detect import (
    Container,
    TocDetectError,
    build_chapters,
    container_by_selector,
    find_page_urls,
    ordered_chapters,
    parse_page,
    pick_container,
)


@dataclass(slots=True)
class DroppedLink:
    reason: str
    text: str
    url: str


@dataclass(slots=True)
class LoadResult:
    toc: Toc
    dropped: list[DroppedLink] = field(default_factory=list)
    container_selector: str = ""
    pages: list[str] = field(default_factory=list)


def load_toc(fetcher: Fetcher, toc_url: str, rules: Rules, selector: str | None = None,
             proxy: str = "") -> LoadResult:
    """抓目录页并返回章节列表。

    只跟随「末页/最大数字页」这一页：分页站点里最新章节在最后一页，
    首页则用于提供稳定的章节总数，两页按 key 合并。
    """
    base_html = fetcher.fetch_html(toc_url, proxy)
    page = parse_page(base_html, toc_url, rules)
    pages: list[tuple[str, Container]] = []
    if selector:
        pages.append((toc_url, container_by_selector(page, selector)))
    else:
        pages.append((toc_url, pick_container(page, rules)))
        followed = _follow_last_page(fetcher, page, pages[0][1], rules, proxy)
        if followed:
            pages.append(followed)

    merged: dict[str, Chapter] = {}
    dropped: list[DroppedLink] = []
    for index, (_url, container) in enumerate(pages):
        for chapter in build_chapters(container.links, position_offset=index * 100_000):
            merged.setdefault(chapter.key, chapter)
        for info in container.dropped:
            dropped.append(DroppedLink(info.drop_reason or "unknown", info.text, info.url))

    chapters = ordered_chapters(list(merged.values()))
    toc = Toc(
        book_title=page.book_title,
        chapters=chapters,
        container_selector=pages[0][1].selector,
        pages_followed=len(pages),
    )
    return LoadResult(toc=toc, dropped=dropped, container_selector=pages[0][1].selector,
                      pages=[url for url, _ in pages])


def _follow_last_page(fetcher: Fetcher, page, container: Container, rules: Rules,
                      proxy: str = ""):
    if rules.cfg.max_pages_followed < 1:
        return None
    last_url, numeric = find_page_urls(page, rules)
    if last_url is None and len(numeric) >= 2:
        last_url = numeric[-1][1]
    if not last_url:
        return None
    try:
        html = fetcher.fetch_html(last_url, proxy)
    except Exception:
        # 分页页拿不到不影响主流程，首页章节照常参与去重
        return None
    second = parse_page(html, last_url, rules)
    try:
        found = pick_container(second, rules, prefer_prefix=container.modal_prefix)
    except TocDetectError:
        return None
    return (last_url, found)
