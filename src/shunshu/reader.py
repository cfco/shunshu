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
             proxy: str = "", html: str | None = None) -> LoadResult:
    """抓目录页并返回章节列表。

    只跟随「末页/最大数字页」这一页：分页站点里最新章节在最后一页，
    首页则用于提供稳定的章节总数，两页按 key 合并。

    html 非 None 时直接用作首页 HTML：添加书源的代理探测刚抓过这一页，
    首轮建档复用它，避免同一个目录页短时间内敲两次。
    """
    base_html = html if html is not None else fetcher.fetch_html(toc_url, proxy)
    page = parse_page(base_html, toc_url, rules)
    pages: list[tuple[str, Container]] = []
    if selector:
        # 手写 selector 也要跟末页：分页站的最新章节就在最后一页，只敲首页永远看不到
        first = container_by_selector(page, selector)
        pages.append((toc_url, first))
        followed = _follow_last_page(fetcher, page, first, rules, proxy,
                                     select=lambda second: container_by_selector(second, selector))
    else:
        first = pick_container(page, rules)
        pages.append((toc_url, first))
        followed = _follow_last_page(fetcher, page, first, rules, proxy)
    if followed:
        pages.append(followed)

    merged: dict[str, Chapter] = {}
    dropped: list[DroppedLink] = []
    # 目录正/倒序判断阈值，从 Config 读取
    order_threshold = rules.cfg.order_trend_threshold
    # 页序就靠 dict 的插入顺序传递（首页的章在前、跟到的那一页在后），
    # 最后由 ordered_chapters 统一重排名次，不需要额外的偏移量
    for _url, container in pages:
        for chapter in build_chapters(container.links, order_threshold=order_threshold):
            merged.setdefault(chapter.key, chapter)
        for info in container.dropped:
            dropped.append(DroppedLink(info.drop_reason or "unknown", info.text, info.url))

    chapters = ordered_chapters(list(merged.values()), order_threshold)
    toc = Toc(
        book_title=page.book_title,
        chapters=chapters,
        container_selector=pages[0][1].selector,
        pages_followed=len(pages),
    )
    return LoadResult(toc=toc, dropped=dropped, container_selector=pages[0][1].selector,
                      pages=[url for url, _ in pages])


def _follow_last_page(fetcher: Fetcher, page, container: Container, rules: Rules,
                      proxy: str = "", select=None):
    """跟末页/最大数字页那一页，拿不到或认不出就当没有第二页，不影响首页结果。

    select 给手写 selector 的分支用（第二页仍按同一个 selector 取容器）；
    默认按首页容器的目录前缀自动打分。
    """
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
        found = select(second) if select is not None else pick_container(
            second, rules, prefer_prefix=container.modal_prefix)
    except TocDetectError:
        return None
    return (last_url, found)
