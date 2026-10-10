from shunshu.models import (
    chinese_to_int,
    normalize_chapter_url,
    parse_chapter_number,
    site_key,
)


def test_chinese_to_int():
    assert chinese_to_int("一千二百三十四") == 1234
    assert chinese_to_int("二十四") == 24
    assert chinese_to_int("一百零五") == 105
    assert chinese_to_int("十三") == 13
    assert chinese_to_int("八千") == 8000
    assert chinese_to_int("1203") == 1203


def test_parse_chapter_number_variants():
    assert parse_chapter_number("第12章 初入江湖") == 12
    assert parse_chapter_number("第一千二百三十四章 大结局") == 1234
    assert parse_chapter_number("第三十一章 中文数字") == 31
    assert parse_chapter_number("1203. 编号式标题") == 1203
    assert parse_chapter_number("1203 空格编号") == 1203
    assert parse_chapter_number("【第8章】方括号") == 8
    assert parse_chapter_number("正文 第9章 承接") == 9
    assert parse_chapter_number("番外 无编号内容") is None


def test_normalize_chapter_url_ignores_scheme_www_query_and_index():
    variants = [
        "http://www.Book.Example.com/12_3456/1.html",
        "https://book.example.com/12_3456/1.html?from=baidu",
        "https://book.example.com/12_3456/1.html#seg1",
        "https://book.example.com/12_3456/1.html/",
    ]
    assert len({normalize_chapter_url(v) for v in variants}) == 1
    assert normalize_chapter_url("https://book.example.com/12_3456/index.html") == \
        normalize_chapter_url("https://book.example.com/12_3456/")


def test_site_key_treats_www_and_m_subdomain_as_same_site():
    assert site_key("https://m.book.example.com/x/1.html") == site_key("https://www.example.com/")
    assert site_key("https://download-elsewhere.net/a.txt") != site_key("https://book.example.com/")


def test_chapter_key_uses_normalized_url():
    from shunshu.models import Chapter

    a = Chapter(title="第1章", url="http://www.x.com/1/2.html?from=baidu#seg")
    b = Chapter(title="第1章 改名", url="https://x.com/1/2.html")
    assert a.key == b.key
    # 跳板式章节的身份就在业务参数上，不能合并成同一章
    c = Chapter(title="第1章", url="https://x.com/b.php?url=/1.html")
    d = Chapter(title="第2章", url="https://x.com/b.php?url=/2.html")
    assert c.key != d.key


def test_normalize_chapter_url_treats_port_as_identity():
    assert normalize_chapter_url("http://127.0.0.1:8765/12_3456/9.html") != \
        normalize_chapter_url("http://127.0.0.1:9000/12_3456/9.html")
    # 默认端口等于没写，不能因为显式写了 :80/:443 就当成另一本书
    assert normalize_chapter_url("https://x.com:443/1/2.html") == normalize_chapter_url("https://x.com/1/2.html")
    assert normalize_chapter_url("http://x.com:80/1/2.html") == normalize_chapter_url("https://x.com/1/2.html")
    # 畸形端口不能让归一化抛异常
    assert normalize_chapter_url("http://x.com:abc/1/2.html") == "https://x.com/1/2.html"


# ---------- 小说与来源 ----------

def make_source(sid: str, url: str, host: str, number: int | None = None,
                title: str = "", enabled: bool = True):
    from shunshu.models import Source

    return Source(id=sid, toc_url=url, site=host, enabled=enabled,
                  latest_number=number, latest_title=title,
                  latest_url=f"{url}{number}.html" if number else None)


def test_novel_find_source_by_id_or_toc_url():
    from shunshu.models import Novel

    a = make_source("a", "https://book.example.com/12_3456/", "book.example.com")
    b = make_source("b", "https://mirror-novel.net/12_3456/", "mirror-novel.net")
    novel = Novel(id="a", sources=[a, b], primary_id="a")
    assert novel.source("b") is b
    assert novel.source(b.toc_url) is b
    assert novel.source("没有这个来源") is None
    assert novel.matches(b.toc_url) is True
    assert novel.primary() is a


def test_novel_latest_chapter_follows_whichever_source_reported_more():
    from shunshu.models import Novel

    a = make_source("a", "https://book.example.com/12_3456/", "book.example.com", 60, "第60章 旧")
    b = make_source("b", "https://mirror-novel.net/12_3456/", "mirror-novel.net", 62, "第62章 新")
    novel = Novel(id="a", title="书", sources=[a, b], primary_id="a")
    assert novel.latest_chapter().number == 62
    b.enabled = False
    assert novel.latest_chapter().number == 60, "暂停的来源不再参与「最新」"
    a.enabled = False
    assert novel.latest_chapter() is None


def test_novel_to_dict_roundtrip_keeps_nested_sources():
    from shunshu.models import Novel

    novel = Novel(id="a", title="书", primary_id="b", read_keys=["000061|风雪夜归人"],
                  sources=[make_source("a", "https://x.com/1/", "x.com", 61, "第61章"),
                           make_source("b", "https://y.com/1/", "y.com", 62, "第62章")])
    restored = Novel.from_dict(novel.to_dict())
    assert restored.primary_id == "b"
    assert [s.id for s in restored.sources] == ["a", "b"]
    assert restored.sources[1].latest_number == 62
    assert restored.read_keys == novel.read_keys
    assert restored.display_title() == "书"


def test_novel_display_title_falls_back_to_primary_host():
    from shunshu.models import Novel

    novel = Novel(id="abcdef123456", sources=[make_source("abcdef123456", "https://x.com/1/", "x.com")])
    assert novel.display_title() == "x.com"
    assert Novel(id="abcdef123456").display_title() == "abcdef123456"


def test_chapter_index_and_alert_ledger_round_trip():
    """小说级的章节索引与提醒账本要原样写进 novels.json 再读回来，重启后不重复弹卡。"""
    from shunshu.models import ChapterEntry, Novel

    a = make_source("a", "https://x.com/1/", "x.com", 62, "第62章")
    b = make_source("b", "https://y.com/1/", "y.com", 61, "第61章")
    novel = Novel(id="a", title="书", sources=[a, b], primary_id="a",
                  read_keys=["000061|风雪夜归"], alerted_keys=["000061|风雪夜归"])
    novel.add_to_index([ChapterEntry(k="000062|新章", t="第62章 新章", u="https://x.com/1/62.html",
                                     s="b", n=62, ts="2026-10-10 09:00:00")])
    assert novel.chapter_index[0].s == "b", "这一章记住的是最先报它的那个来源"
    assert novel.site_of("b") == "y.com", "卡片行上显示的是这个来源的站点名"

    restored = Novel.from_dict(novel.to_dict())
    entry = restored.chapter_index[0]
    assert isinstance(entry, ChapterEntry)
    assert (entry.k, entry.t, entry.u, entry.s, entry.n, entry.ts) == (
        "000062|新章", "第62章 新章", "https://x.com/1/62.html", "b", 62, "2026-10-10 09:00:00")
    assert restored.read_keys == novel.read_keys
    assert restored.alerted_keys == novel.alerted_keys
    assert restored.site_of(entry.s) == "y.com"


def test_add_to_index_never_reassigns_a_chapter_to_a_later_source():
    """同一章后来的来源只能补充，不能改写归属；索引按新在前截断。"""
    from shunshu.models import ChapterEntry, Novel

    novel = Novel(id="a", title="书", sources=[make_source("a", "https://x.com/1/", "x.com")])
    novel.add_to_index([ChapterEntry(k="000061|第一章", s="a"),
                        ChapterEntry(k="000060|序章", s="a")])
    novel.add_to_index([ChapterEntry(k="000061|第一章", s="b"),
                        ChapterEntry(k="000063|第三章", s="b")])
    assert [e.k for e in novel.chapter_index] == ["000063|第三章", "000061|第一章", "000060|序章"]
    kept = next(e for e in novel.chapter_index if e.k == "000061|第一章")
    assert kept.s == "a", "归属按最先报它的来源，后来的来源不能改写"
    assert novel.site_of(kept.s) == "x.com"

    # 新在前由调用方保证（collect_alerts 已按章节号排好），这里只验截断长度
    novel.add_to_index([ChapterEntry(k=f"0000{i}|补", s="b") for i in (98, 97, 96)], rows=3)
    assert [e.k for e in novel.chapter_index] == ["000098|补", "000097|补", "000096|补"]


def test_mark_alerted_keeps_only_the_most_recent_keys():
    from shunshu.models import Novel

    novel = Novel(id="a", title="书", sources=[make_source("a", "https://x.com/1/", "x.com")])
    novel.mark_alerted([f"k{i}" for i in range(10)], keep=4)
    assert novel.alerted_keys == ["k6", "k7", "k8", "k9"]
    novel.mark_alerted(["k6", "k10"], keep=4)
    assert novel.alerted_keys == ["k7", "k8", "k9", "k10"], "已有的键要挪到最新位置，不重复记"


def test_novel_display_title_is_simplified():
    """书名也统一简体显示，存下来的仍是源站原文。"""
    from shunshu.models import Novel

    novel = Novel(id="a", title="陣問長生", sources=[make_source("a", "https://x.com/1/", "x.com")])
    assert novel.display_title() == "阵问长生"
    assert novel.title == "陣問長生"
