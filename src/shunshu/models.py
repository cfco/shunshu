"""数据模型与章节身份归一化。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from posixpath import normpath
from urllib.parse import urlsplit

STATUS_BASELINE = "baseline"
STATUS_UPDATED = "updated"
STATUS_NO_CHANGE = "no_change"
STATUS_TOC_RESET = "toc_reset"
STATUS_PARSE_FAILED = "parse_failed"
STATUS_NET_ERROR = "net_error"
STATUS_PROXY_ERROR = "proxy_error"
STATUS_DISABLED = "disabled"
STATUS_CHECK_ERROR = "check_error"

_LABELS = {
    STATUS_BASELINE: "首次记录",
    STATUS_UPDATED: "有更新",
    STATUS_NO_CHANGE: "无更新",
    STATUS_TOC_RESET: "目录疑似重置",
    STATUS_PARSE_FAILED: "解析失败",
    STATUS_NET_ERROR: "网络错误",
    STATUS_PROXY_ERROR: "代理不可用",
    STATUS_DISABLED: "已暂停",
    STATUS_CHECK_ERROR: "检查异常",
}

_RANGE_RE = re.compile(r"^(\d+)\s*[-~—]\s*\d+$")
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "两": 2,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    # 繁体站的大写数字
    "貳": 2, "參": 3, "陸": 6,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "拾": 10, "佰": 100, "仟": 1000, "廿": 20, "卅": 30}
_CN_BIGS = {"万": 10_000, "萬": 10_000, "亿": 100_000_000, "億": 100_000_000}

# 章节号字符集只在这里定义一次，解析和「像不像章节」两条正则共用，避免两边各自漏字
CN_NUM_CHARS = "".join(sorted({*_CN_DIGITS, *_CN_UNITS, *_CN_BIGS}))
CN_LABEL_CHARS = "章节節回篇卷折幕"

_TITLE_NUM_RE = re.compile(
    rf"^[\s【\[（(]*(?:正文|番外|卷首)?[\s】\]）)]*\s*第\s*([0-9{re.escape(CN_NUM_CHARS)}]+)\s*[{CN_LABEL_CHARS}]"
)
_PREFIX_NUM_RE = re.compile(r"^\s*(\d{1,6})\s*(?:[\s、.．:：_\-–—]|$)")


def status_label(status: str) -> str:
    return _LABELS.get(status, status)


def _str_list(value) -> list[str]:
    """手改的名单字段统一成字符串列表。

    裸字符串当一条收下（README 让人「把该章 URL 填进 keep_keys」，漏打方括号是最常见的写法）；
    null 与对象当空；列表里的数字转成文本，嵌套对象丢掉。
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def chinese_to_int(text: str) -> int | None:
    """把「一千二百三十四」「二十四」这类中文数字转成整数，失败返回 None。"""
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    number = 0
    seen = False
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            section += (number or 1) * unit
            number = 0
            seen = True
        elif ch in _CN_BIGS:
            big = _CN_BIGS[ch]
            section = (section + number) * big
            total += section
            section = 0
            number = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section + number


def parse_chapter_number(title: str) -> int | None:
    """从章节标题里解析章节序号，用于判断「哪一章最新」而不是依赖页面位置。"""
    m = _TITLE_NUM_RE.match(title)
    if m:
        raw = m.group(1)
        value = chinese_to_int(raw)
        if value is not None:
            return value
    m = _PREFIX_NUM_RE.match(title)
    if m:
        return int(m.group(1))
    return None


def strip_chapter_label(title: str) -> str:
    """剥掉「第120章 」「1001. 」这类序号前缀，只留正题。跨源比对用。

    各站前缀写法不一（有的带「正文」、有的编号后接全角点），正题才是稳定信号。
    """
    text = (title or "").strip()
    for pattern in (_TITLE_NUM_RE, _PREFIX_NUM_RE):
        matched = pattern.match(text)
        if matched:
            return text[matched.end():].strip()
    return text


def strip_www(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


_PUBLIC_SUFFIXES = {"com", "net", "org", "edu", "gov", "co", "ac", "or", "ne", "in", "ltd", "ipla"}


def site_key(url: str) -> str:
    """粗略的注册域名，用于判断章节链接是否跳去了外站广告。"""
    host = strip_www(urlsplit(url).hostname or "")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail = labels[-2:]
    if len(labels) >= 3 and tail[0] in _PUBLIC_SUFFIXES:
        tail = labels[-3:]
    return ".".join(tail)


def host_label(url: str) -> str:
    """来源显示名用完整主机名：同一注册域下的两个镜像站得能一眼分清。"""
    return strip_www(urlsplit(url or "").hostname or "")


_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm", "from",
    "refer", "referer", "ref", "share", "source", "sign", "signature", "timestamp", "ts",
    "tk", "af", "pid", "cid", "gt", "scene", "ch", "channel", "isappinstalled",
}


def _clean_query(query: str) -> str:
    """丢跟踪参数、留业务参数：跳板式章节链接（b.php?url=…）的身份就在 query 上。"""
    kept: list[tuple[str, str]] = []
    for pair in (query or "").split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        if key.lower() in _TRACKING_QUERY_KEYS:
            continue
        kept.append((key, value))
    return "&".join(f"{key}={value}" for key, value in sorted(kept))


def normalize_chapter_url(url: str) -> str:
    """章节身份用的归一化 URL：去协议差异、去 www、去 fragment 与跟踪参数、去 index.html。

    站点常给同一章节多种链接形态，归一化后才能稳定去重。
    """
    parts = urlsplit((url or "").strip())
    host = strip_www(parts.hostname or "")
    try:
        port = parts.port
    except ValueError:
        port = None
    path = normpath(parts.path or "/") if parts.path else "/"
    for suffix in ("/index.html", "/index.htm", "/index.shtml", "/default.html"):
        if path.endswith(suffix) and len(path) > len(suffix):
            path = path[: -len(suffix)]
            break
    if len(path) > 1:
        path = path.rstrip("/")
    query = _clean_query(parts.query)
    # 端口是身份的一部分：8765 和 9000 上是两本不同的书，只有默认端口可以省
    netloc = f"{host}:{port}" if port and port not in (80, 443) else host
    return f"https://{netloc}{path}" + (f"?{query}" if query else "")


@dataclass(slots=True)
class Chapter:
    title: str
    url: str
    number: int | None = None
    position: int = 0
    raw_title: str = ""

    @property
    def key(self) -> str:
        return normalize_chapter_url(self.url)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.number if self.number is not None else -1, self.position)

    def to_dict(self) -> dict:
        return {"t": self.title, "u": self.url, "n": self.number}


@dataclass(slots=True)
class Candidate:
    """一个候选章节容器，识别失败时写进 data/candidates/ 供人工确认。"""

    selector: str
    links: int
    chapterish: int
    ads: int
    external: int
    score: float
    samples: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"CSS: {self.selector}\n"
            f"  链接 {self.links} 个 / 章节样式 {self.chapterish} 个 / 广告 {self.ads} / 外链 {self.external} / 得分 {self.score:.1f}\n"
            f"  样例: {' | '.join(self.samples)}"
        )


@dataclass(slots=True)
class Toc:
    book_title: str | None
    chapters: list[Chapter]
    container_selector: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    pages_followed: int = 1

    @property
    def latest(self) -> Chapter | None:
        """最新章节＝目录排列方向上的最后一条（`ordered_chapters` 已排成旧→新）。"""
        return self.chapters[-1] if self.chapters else None


@dataclass(slots=True)
class Source:
    """一个抓取来源：同一本小说在某个站点上的那个目录页。"""

    id: str
    toc_url: str
    site: str = ""
    title: str = ""
    selector: str | None = None
    proxy: str = ""
    keep_keys: list[str] = field(default_factory=list)
    drop_keys: list[str] = field(default_factory=list)
    enabled: bool = True
    baseline_set: bool = False
    latest_title: str | None = None
    latest_url: str | None = None
    latest_number: int | None = None
    notified_keys: list[str] = field(default_factory=list)
    chapter_count: int = 0
    pages_followed: int = 1
    status: str = STATUS_BASELINE
    status_detail: str = ""
    last_checked: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    created_at: str | None = None

    def known_keys(self) -> set[str]:
        """来源级去重按归一化 URL：同一个站的目录不会把同一章列两遍。"""
        return set(self.notified_keys)

    def latest_chapter(self) -> Chapter | None:
        if not self.latest_url:
            return None
        return Chapter(title=self.latest_title or "", url=self.latest_url, number=self.latest_number)

    def matches(self, ident: str) -> bool:
        return ident in (self.id, self.toc_url)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Source:
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        # novels.json 是要给人手改的：这几个字段写成 null、数字或漏了引号，都只是这一条不好使，
        # 不该让 AttributeError 冒到加载循环外面把整个工具拦在启动阶段
        for key in ("id", "toc_url", "site", "title", "proxy"):
            payload[key] = str(payload.get(key) or "")
        if payload.get("selector"):
            payload["selector"] = str(payload["selector"])
        for key in ("keep_keys", "drop_keys", "notified_keys"):
            payload[key] = _str_list(payload.get(key))
        return cls(**payload)


@dataclass(slots=True)
class ChapterEntry:
    """小说级章节索引的一行：这一章是谁最先报上来的。"""

    k: str
    t: str = ""
    u: str = ""
    s: str = ""
    n: int | None = None
    ts: str = ""

    def chapter(self) -> Chapter:
        return Chapter(title=self.t, url=self.u, number=self.n)


@dataclass(slots=True)
class Novel:
    """书架上的一本：一个小说挂着若干来源，多来源共同维护它的更新与已读。"""

    id: str
    title: str = ""
    sources: list[Source] = field(default_factory=list)
    primary_id: str = ""
    read_keys: list[str] = field(default_factory=list)
    alerted_keys: list[str] = field(default_factory=list)
    chapter_index: list[ChapterEntry] = field(default_factory=list)
    created_at: str | None = None

    def source(self, ident: str) -> Source | None:
        for source in self.sources:
            if source.matches(ident):
                return source
        return None

    def site_of(self, source_id: str) -> str:
        source = self.source(source_id)
        return (source.site if source else "") or source_id

    def primary(self) -> Source | None:
        return self.source(self.primary_id) or (self.sources[0] if self.sources else None)

    def display_title(self) -> str:
        from .align import display_title  # 延迟导入：align 依赖本模块

        stored = self.title or (self.primary().site if self.sources else "") or self.id
        return display_title(stored)

    def latest_chapter(self) -> Chapter | None:
        """跨来源取最新：主源挂了或还没更，其它源的最新章节照样能打开。"""
        candidates = [s.latest_chapter() for s in self.sources if s.enabled]
        candidates = [c for c in candidates if c is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.sort_key)

    def mark_alerted(self, keys: list[str], keep: int = 500) -> None:
        existing = set(self.alerted_keys)
        merged = self.alerted_keys + [k for k in keys if k not in existing]
        # keep=0 表示一个都不留：[-0:] 会退化成 [0:] 把整份留下来，必须显式清空
        self.alerted_keys = merged[-keep:] if keep > 0 else []

    def add_to_index(self, entries: list[ChapterEntry], rows: int = 50) -> None:
        """索引只留最近 `rows` 章，新在前；同一章保留最先报它的那个来源。"""
        known = {e.k for e in self.chapter_index}
        fresh = [e for e in entries if e.k not in known]
        self.chapter_index = (fresh + self.chapter_index)[:rows]

    def matches(self, ident: str) -> bool:
        if ident in (self.id, self.title):
            return True
        return any(s.matches(ident) for s in self.sources)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Novel:
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        payload["title"] = str(payload.get("title") or "")
        payload["primary_id"] = str(payload.get("primary_id") or "")
        payload["id"] = str(payload.get("id") or "")
        # 来源被写成字符串（少打一对花括号）时丢掉那一条，别把整本带下水
        raw_sources = payload.get("sources")
        payload["sources"] = [Source.from_dict(s) for s in
                              (raw_sources if isinstance(raw_sources, list) else [])
                              if isinstance(s, dict)]
        for key in ("read_keys", "alerted_keys"):
            payload[key] = _str_list(payload.get(key))
        raw_index = payload.get("chapter_index")
        entries = []
        for entry in (raw_index if isinstance(raw_index, list) else []):
            # k 是跨源身份，没有它这行就没法去重，单行跳过
            if isinstance(entry, dict) and entry.get("k"):
                entries.append(ChapterEntry(k=str(entry["k"]), t=str(entry.get("t") or ""),
                                            u=str(entry.get("u") or ""), s=str(entry.get("s") or ""),
                                            n=entry.get("n"), ts=str(entry.get("ts") or "")))
        payload["chapter_index"] = entries
        return cls(**payload)


@dataclass(slots=True)
class Outcome:
    """一个来源一次检查的结果，直接对应「弹窗该说什么」。"""

    novel: Novel
    source: Source
    status: str
    new_chapters: list[Chapter] = field(default_factory=list)
    recent: list[Chapter] = field(default_factory=list)
    new_count: int = 0
    latest: Chapter | None = None
    message: str = ""
    error: str | None = None
    # 本轮这个来源全部的新一章：new_chapters 是给日志看的截断版，跨来源去重不能用它
    all_new: list[Chapter] = field(default_factory=list)
    # 基线时这次建档认下来的全部章节（match_key）：别的来源后来才看到这些章，不能当更新报
    known_keys: list[str] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        """新章节总数：new_chapters 受显示条数上限截断，计数不能用它。"""
        return self.new_count or len(self.new_chapters)

    def detail(self) -> str:
        """「状态：原因」一行话：原因本身已经带状态名时不重复念一遍。"""
        label = status_label(self.status)
        if not self.message:
            return label
        return self.message if self.message.startswith(label) else f"{label}：{self.message}"

    def summary(self) -> str:
        """启动摘要/日志用的一行文本：无更新报最后一章，有更新逐章列标题。"""
        from .align import display_title  # 延迟导入：align 依赖本模块

        title = self.novel.display_title()
        if self.status == STATUS_UPDATED:
            names = "、".join(display_title(c.title) for c in self.new_chapters)
            suffix = f"（共 {self.total_new} 章）" if self.total_new > len(self.new_chapters) else ""
            return f"{title}: 更新 {self.total_new} 章 → {names}{suffix}"
        if self.status == STATUS_BASELINE:
            last = f"《{display_title(self.latest.title)}》" if self.latest else "未取到章节"
            return f"{title}: 首次记录，最后一章 {last}（不提醒）"
        if self.status == STATUS_NO_CHANGE:
            last = f"《{display_title(self.latest.title)}》" if self.latest else "未知"
            return f"{title}: 无更新，最后一章 {last}"
        if self.status == STATUS_TOC_RESET:
            return f"{title}: {self.message}"
        return f"{title}: {self.message or status_label(self.status)}"


@dataclass(slots=True)
class NovelAlert:
    """一轮检查里，一本小说真正要提醒的东西：多个来源合成一张卡片，同一章只报一次。"""

    novel: Novel
    entries: list[ChapterEntry] = field(default_factory=list)
    added: bool = False
    latest: Chapter | None = None

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def sites(self) -> list[str]:
        """这张卡片里的章节由哪些来源报上来的，按最先报的那一章排；同一站两个来源也不会混。"""
        labels: list[str] = []
        for entry in self.entries:
            label = self.novel.site_of(entry.s)
            if label and label not in labels:
                labels.append(label)
        return labels

    def title(self) -> str:
        name = self.novel.display_title()
        if self.added:
            return f"已添加《{name}》"
        base = f"《{name}》更新 {self.count} 章"
        origin = self.origin_text()
        return f"{base}（{origin}）" if origin else base

    def origin_text(self) -> str:
        """卡片标题里点明是谁报的；主源没更而别的源更了，要说明白。"""
        sites = self.sites
        if not sites:
            return ""
        joined = "、".join(sites)
        primary = self.novel.primary()
        reported = {entry.s for entry in self.entries}
        if primary and len(self.novel.sources) > 1 and primary.id not in reported:
            return f"来自 {joined}，主源未更新"
        return f"来自 {joined}"

    def summary(self) -> str:
        from .align import display_title

        if self.added:
            return f"已添加《{self.novel.display_title()}》"
        names = "、".join(display_title(e.t) for e in self.entries)
        return f"《{self.novel.display_title()}》更新 {self.count} 章 → {names}"
