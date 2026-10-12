"""从目录页 HTML 里认出章节容器，并把盗版站的广告链接剔掉。

本模块只做解析，不发请求；抓取与分页合并在 reader.py。
流水线：逐链接分类 → 候选容器打分 → 取最优容器 → 章节号解析与排序。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .config import AD_NODE_HINT, CHAPTER_HINT, Rules, normalize_text
from .models import Candidate, Chapter, normalize_chapter_url, parse_chapter_number, site_key, strip_www

_PAGE_TEXT_RE = re.compile(r"^\s*(\d{1,4})\s*$")
_LAST_PAGE_TEXTS = {"末页", "最后一页", "最后", "末"}
_NEXT_PAGE_TEXTS = {"下一页", "下页", "next", "下頁", "下一頁"}
_PAGE_NAV_TEXTS = {"首页", "上一页", "下一页", "末页", "下页", "上页", "next", "prev", "last"}
_PAGE_FILE_RE = re.compile(r"(?:^|/)_+\d{1,4}(?:\.\w+)?$", re.I)
_PAGE_QUERY_RE = re.compile(r"(?:page|p|pg|id_page)=\d+", re.I)
_PAGE_DIR_RE = re.compile(r"/\d{1,4}/$")
_PAGE_HINT_CLASS = re.compile(r"(?:^|[\s,_-])(?:pages?|fenye|pag(?:e)?_?bar|bar|pagebreak)(?:$|[\s,_-])", re.I)
_SKIP_TAGS = {"script", "style", "noscript", "textarea", "svg"}
_FILE_SUFFIX_RE = re.compile(r"\.(?:html?|shtml|jhtml|php|aspx?)$", re.I)
_TITLE_SPLIT_RE = re.compile(r"[_|—/《》\[\]（）()]+")


class TocDetectError(Exception):
    """识别不出章节容器；candidates 带 Top-N 容器描述，供人工写 selector。"""

    def __init__(self, message: str, candidates: list[Candidate]):
        super().__init__(message)
        self.candidates = candidates


@dataclass(slots=True)
class LinkInfo:
    anchor: Tag
    raw_text: str
    text: str
    url: str
    number: int | None
    chapterish: bool
    dir_prefix: str
    drop_reason: str | None = None

    @property
    def kept(self) -> bool:
        return self.drop_reason is None


@dataclass(slots=True)
class Page:
    url: str
    soup: BeautifulSoup
    links: list[LinkInfo]
    by_anchor: dict[int, LinkInfo]
    book_title: str | None


@dataclass(slots=True)
class Container:
    selector: str
    links: list[LinkInfo]
    modal_prefix: str

    @property
    def kept(self) -> list[LinkInfo]:
        return [l for l in self.links if l.kept]

    @property
    def dropped(self) -> list[LinkInfo]:
        return [l for l in self.links if not l.kept]


def display_text(anchor: Tag) -> str:
    text = anchor.get_text(" ", strip=True) or (anchor.get("title") or "")
    return normalize_display(text)


def normalize_display(text: str) -> str:
    text = (text or "").translate(_DISPLAY_TRANS)
    return re.sub(r"\s+", " ", text).strip()


_DISPLAY_TRANS = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x00A0, 0x3000], None
)


def clean_title(raw: str, rules: Rules) -> str:
    text = normalize_display(raw)
    for _ in range(3):
        before = text
        for pat in rules.suffixes:
            text = pat.sub("", text)
        if text == before:
            break
    return text.strip()


def _in_junk_node(anchor: Tag) -> bool:
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            return False
        if node.name in _SKIP_TAGS:
            return True
        classes = node.get("class") or []
        hint = " ".join([*classes, node.get("id") or ""])
        if hint.strip() and AD_NODE_HINT.search(hint):
            return True
    return False


def dir_prefix(url: str) -> str:
    path = urlsplit(url).path
    return path.rsplit("/", 1)[0] + "/" if "/" in path else "/"


def classify_anchor(anchor: Tag, page_url: str, rules: Rules) -> LinkInfo:
    raw = display_text(anchor)
    text = clean_title(raw, rules)
    href = (anchor.get("href") or "").strip()
    url = urljoin(page_url, href) if href else ""
    number = parse_chapter_number(text)
    chapterish = number is not None or bool(CHAPTER_HINT.search(text))
    info = LinkInfo(
        anchor=anchor, raw_text=raw, text=text, url=url, number=number,
        chapterish=chapterish, dir_prefix=dir_prefix(url) if url else "",
    )
    info.drop_reason = _drop_reason(info, page_url, rules)
    return info


def _is_page_link(info: LinkInfo, page_url: str) -> bool:
    """目录分页链接：不是章节，否则「下一页」会被当成一章反复提醒。"""
    norm = normalize_text(info.text)
    if norm in _PAGE_NAV_TEXTS or norm in _LAST_PAGE_TEXTS:
        return True
    if not _PAGE_TEXT_RE.match(info.text):
        return False
    parts = urlsplit(info.url)
    if _PAGE_QUERY_RE.search(parts.query or ""):
        return True
    if _PAGE_FILE_RE.search(parts.path):
        return True
    if _PAGE_DIR_RE.search(parts.path) and site_key(info.url) == site_key(page_url):
        return True
    return bool(info.anchor.find_parent(class_=_PAGE_HINT_CLASS) or
                info.anchor.find_parent(id_=_PAGE_HINT_CLASS))


def _drop_reason(info: LinkInfo, page_url: str, rules: Rules) -> str | None:
    href = (info.anchor.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith(("javascript:", "vbscript:", "data:")):
        return "bad_href"
    parts = urlsplit(info.url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "bad_scheme"
    if site_key(info.url) != site_key(page_url) and strip_www(parts.hostname) not in rules.whitelist:
        return "external_host"
    if _in_junk_node(info.anchor):
        return "ad_container"
    if _is_page_link(info, page_url):
        return "pagination"
    norm = normalize_text(info.text)
    if not norm:
        return "empty_text"
    if norm in rules.exact:
        return "functional_text"
    # 章节样式的标题只走精确名单：「第3章（求月票）」是真章节，不能被推广词误杀
    if not info.chapterish:
        for pat in rules.patterns:
            if pat.search(info.text):
                return "ad_text"
    return None


def selector_of(node: Tag) -> str:
    parts: list[str] = []
    cur: Tag | None = node
    while cur is not None and cur.name not in (None, "html", "[document]"):
        piece = cur.name
        if cur.get("id"):
            piece += f"#{cur['id']}"
        else:
            classes = [c for c in (cur.get("class") or []) if c]
            if classes:
                piece += "." + ".".join(classes[:2])
        parts.append(piece)
        cur = cur.parent
    return " > ".join(reversed(parts))


def _depth(container: Tag, node: Tag) -> int:
    depth = 0
    cur: Tag | None = node
    while cur is not None and cur is not container:
        depth += 1
        cur = cur.parent
    return -1 if cur is None else depth


def _ancestor_buckets(page: Page) -> list[Tag]:
    seen: set[int] = set()
    out: list[Tag] = []
    for info in page.links:
        node = info.anchor.parent
        for _ in range(3):
            if node is None or not isinstance(node, Tag):
                break
            if id(node) not in seen:
                seen.add(id(node))
                out.append(node)
            node = node.parent
    return out


def _links_in(container: Tag, page: Page) -> list[LinkInfo]:
    out: list[LinkInfo] = []
    for anchor in container.find_all("a"):
        info = page.by_anchor.get(id(anchor))
        if info is None:
            continue
        if _depth(container, anchor) > 3:
            continue
        out.append(info)
    return out


def score_links(links: list[LinkInfo]) -> tuple[float, int, int, int, int, float]:
    total = len(links)
    if total == 0:
        return -1.0, 0, 0, 0, 0, 0.0
    kept = [l for l in links if l.kept]
    kept_chapterish = [l for l in kept if l.chapterish]
    ads = sum(1 for l in links if l.drop_reason in ("functional_text", "ad_text", "ad_container", "empty_text"))
    external = sum(1 for l in links if l.drop_reason in ("external_host", "bad_scheme", "bad_href"))
    coverage = len(kept_chapterish) / total
    if kept:
        modal = Counter(dir_prefix(l.url) for l in kept).most_common(1)[0][0]
        same_prefix = sum(1 for l in kept if dir_prefix(l.url) == modal) / len(kept)
    else:
        modal, same_prefix = "", 0.0
    score = (
        len(kept_chapterish) * (1.0 + coverage)
        + 2.0 * same_prefix * len(kept_chapterish)
        - 3.0 * ads
        - 4.0 * external
    )
    return score, total, len(kept_chapterish), ads, external, same_prefix


def _accept(links: list[LinkInfo], rules: Rules, follow: bool = False) -> bool:
    kept = [l for l in links if l.kept]
    chapterish = sum(1 for l in kept if l.chapterish)
    if follow:
        # 分页跟随页可能只有零星几章，只要还有本站链接就认
        return bool(kept)
    if chapterish >= rules.cfg.min_chapterish_links:
        return True
    # 无编号章节的书：靠「同目录前缀 + 链接量」结构特征兜底
    if len(kept) >= 20:
        modal = Counter(dir_prefix(l.url) for l in kept).most_common(1)[0][1]
        return modal / len(kept) >= 0.8
    return False


def _candidate_note(container: Tag, links: list[LinkInfo], score: float, total: int,
                     chapterish: int, ads: int, external: int) -> Candidate:
    samples = [l.text for l in links if l.kept][:3] or [l.text for l in links][:3]
    return Candidate(
        selector=selector_of(container), links=total, chapterish=chapterish,
        ads=ads, external=external, score=round(score, 1), samples=samples,
    )


def pick_container(page: Page, rules: Rules, prefer_prefix: str | None = None) -> Container:
    """挑出最像章节目录的容器；prefer_prefix 用于分页时沿用首页容器的路径特征。"""
    follow = prefer_prefix is not None
    # 分页跟随页往往只有几章，只要有链接就能参与；自动识别至少要 2 条才值得记录候选
    min_links = 1 if follow else 2
    scored: list[tuple[float, int, Tag, list[LinkInfo], float]] = []
    notes: list[Candidate] = []
    for bucket in _ancestor_buckets(page):
        links = _links_in(bucket, page)
        if len(links) < min_links:
            continue
        score, total, chapterish, ads, external, same_prefix = score_links(links)
        if follow:
            matched = sum(1 for l in links if l.kept and dir_prefix(l.url) == prefer_prefix)
            if matched == 0:
                continue
            score += 3.0 * matched
        notes.append(_candidate_note(bucket, links, score, total, chapterish, ads, external))
        # 候选笔记用于失败时给人看；自动参赛才要求「链接够多」，分页跟随页按前缀放行
        if follow or total >= rules.cfg.min_container_links:
            scored.append((score, total, bucket, links, same_prefix))
    scored.sort(key=lambda t: (-t[0], t[1]))
    notes.sort(key=lambda c: -c.score)
    for score, _total, bucket, links, _same in scored:
        if _accept(links, rules, follow=follow):
            kept = [l for l in links if l.kept]
            modal = Counter(dir_prefix(l.url) for l in kept).most_common(1)[0][0] if kept else ""
            return Container(selector=selector_of(bucket), links=links, modal_prefix=modal)
    raise TocDetectError(
        "没能确定章节容器：没有候选容器达到「章节样式链接足够多」的门槛",
        [n for n in notes if n.links >= 5][:3] or notes[:3],
    )


def container_by_selector(page: Page, selector: str) -> Container:
    """手写 selector 模式：匹配到容器元素（或单个 a）就用它，不再自动打分。"""
    for node in page.soup.select(selector):
        if not isinstance(node, Tag):
            continue
        if node.name == "a":
            info = page.by_anchor.get(id(node))
            links = [info] if info else []
            target = node
        else:
            links = _links_in(node, page)
            target = node
        if not links:
            continue
        kept = [l for l in links if l.kept]
        modal = Counter(dir_prefix(l.url) for l in kept).most_common(1)[0][0] if kept else ""
        return Container(selector=selector_of(target), links=links, modal_prefix=modal)
    raise TocDetectError(f"手写的 selector 没有匹配到含链接的元素: {selector}", [])


def build_chapters(links: list[LinkInfo], order_threshold: float = 0.6) -> list[Chapter]:
    """按 key 去重后交给 `ordered_chapters` 排新旧：目录里的出现顺序才是站点的说法。"""
    chapters: dict[str, Chapter] = {}
    for pos, info in enumerate(links):
        if not info.kept or not info.text:
            continue
        key = normalize_chapter_url(info.url)
        if key in chapters:
            continue
        chapters[key] = Chapter(
            title=info.text, url=info.url, number=info.number,
            position=pos, raw_title=info.raw_text,
        )
    return ordered_chapters(list(chapters.values()), order_threshold)


def ordered_chapters(chapters: list[Chapter], order_threshold: float = 0.6) -> list[Chapter]:
    """把「按目录出现顺序」的章节排成旧→新，并把 position 重排成名次（0 最旧）。

    站点自己把编号写乱时（正文排到 1564 章，末尾又另挂一块 184~190 章，
    那块才是最新）不能按编号排——认目录顺序，只用相邻编号的升降投票判断这本书
    是正序排还是倒序排。order_threshold 控制投票阈值，从 Config 传入。
    """
    trend = _order_trend(chapters, order_threshold)
    if trend == 0 and len(chapters) > 1 and all(c.number is not None for c in chapters):
        ordered = sorted(chapters, key=lambda c: (c.number or 0, c.position))
    else:
        ordered = list(reversed(chapters)) if trend < 0 else list(chapters)
    for rank, chapter in enumerate(ordered):
        chapter.position = rank
    return ordered


def _order_trend(chapters: list[Chapter], threshold: float = 0.6) -> int:
    """沿目录顺序编号整体在涨返回 +1，整体在跌（倒序站）返回 -1，看不出方向返回 0。"""
    nums = [c.number for c in chapters if c.number is not None]
    if len(nums) < 4:
        return 0
    up = sum(1 for a, b in zip(nums, nums[1:]) if b > a)
    down = sum(1 for a, b in zip(nums, nums[1:]) if b < a)
    total = up + down
    if not total:
        return 0
    if up / total > threshold:
        return 1
    if down / total > threshold:
        return -1
    return 0


def detect_book_title(soup: BeautifulSoup, rules: Rules) -> str | None:
    h1 = soup.find("h1")
    if h1:
        source = h1.find("a") or h1
        text = clean_title(display_text(source), rules)
        if 1 <= len(text) <= 40 and not CHAPTER_HINT.search(text) and normalize_text(text) not in rules.exact:
            return text
    tag = soup.find("title")
    if tag:
        for piece in _TITLE_SPLIT_RE.split(display_text(tag)):
            text = clean_title(piece, rules)
            if not (1 <= len(text) <= 30):
                continue
            if CHAPTER_HINT.search(text) or normalize_text(text) in rules.exact:
                continue
            if any(word in text for word in ("目录", "最新章节", "小说", "在线阅读", "笔趣阁", "无弹窗", "阅读")):
                continue
            if any(pat.search(text) for pat in rules.patterns):
                continue
            return text
    return None


def parse_page(html: str, page_url: str, rules: Rules) -> Page:
    soup = BeautifulSoup(html, "lxml")
    links: list[LinkInfo] = []
    by_anchor: dict[int, LinkInfo] = {}
    for anchor in soup.find_all("a"):
        info = classify_anchor(anchor, page_url, rules)
        links.append(info)
        by_anchor[id(anchor)] = info
    return Page(url=page_url, soup=soup, links=links, by_anchor=by_anchor, book_title=detect_book_title(soup, rules))


def find_page_urls(page: Page, rules: Rules) -> tuple[str | None, list[tuple[int, str]]]:
    """返回 (该跟进的那一页, 数字页链接列表)。

    优先「末页」文字链接，其次最大数字页；两样都没有时退回「下一页」——
    只挂一个「下一页」的两页站，不跟这一页就永远看不到最新章节。
    """
    numeric: dict[int, str] = {}
    last: str | None = None
    nxt: str | None = None
    base = urlsplit(page.url)
    for info in page.links:
        if not info.url or site_key(info.url) != site_key(page.url):
            continue
        parts = urlsplit(info.url)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
            continue
        if not (parts.path.endswith("/") or _FILE_SUFFIX_RE.search(parts.path)):
            continue
        norm = normalize_text(info.text)
        if norm in _LAST_PAGE_TEXTS:
            last = info.url
            continue
        if norm in _NEXT_PAGE_TEXTS and nxt is None and dir_prefix(info.url) == dir_prefix(page.url):
            # 只认同目录的翻页：/module.php?go=next 这类假「下一页」跟着敲一次门是白敲
            nxt = info.url
            continue
        m = _PAGE_TEXT_RE.match(info.text)
        if m and abs(len(parts.path) - len(base.path)) <= 8:
            num = int(m.group(1))
            if 1 <= num <= 2000:
                numeric[num] = info.url
    candidates = sorted(numeric.items())
    here = normalize_chapter_url(page.url)
    for candidate in (last, nxt):
        if candidate and normalize_chapter_url(candidate) != here:
            return candidate, candidates
    return None, candidates
