import fixtures
from conftest import FakeFetcher

from shunshu.config import Config
from shunshu.http import FetchError, ProxyError
from shunshu.models import (
    STATUS_BASELINE,
    STATUS_NET_ERROR,
    STATUS_PROXY_ERROR,
    STATUS_NO_CHANGE,
    STATUS_TOC_RESET,
    STATUS_UPDATED,
    Novel,
    Source,
    normalize_chapter_url,
    status_label,
)
from shunshu.monitor import check_source, collect_alerts
from shunshu.reader import load_toc
from shunshu.store import read_jsonl, source_id_for

RULES = Config().compiled()


def make_novel(url: str = fixtures.BASE_URL) -> Novel:
    """一本小说挂一个来源：检查逻辑都是按来源跑的。"""
    source = Source(id=source_id_for(url), toc_url=url, site="book.example.com")
    return Novel(id=source.id, title="", sources=[source], primary_id=source.id)


def first(novel: Novel) -> Source:
    return novel.sources[0]


def toc(pages: dict[str, str]):
    return load_toc(FakeFetcher(pages), fixtures.BASE_URL, RULES)


def test_load_toc_merges_first_and_last_page():
    result = toc({
        fixtures.BASE_URL: fixtures.paged_first_html(),
        fixtures.BASE_URL + "_20.html": fixtures.paged_last_html(),
    })
    numbers = [c.number for c in result.toc.chapters]
    assert 1 in numbers and 60 in numbers and 600 in numbers
    assert result.toc.chapters[-1].title == "第六百章 大结局"
    assert result.pages == [fixtures.BASE_URL, fixtures.BASE_URL + "_20.html"]


def test_load_toc_survives_missing_last_page():
    result = toc({fixtures.BASE_URL: fixtures.paged_first_html()})
    assert len(result.toc.chapters) == 60


def test_first_check_creates_baseline_without_notifying():
    novel = make_novel()
    outcome = check_source(novel, first(novel), RULES,
                           FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}), None)
    assert outcome.status == STATUS_BASELINE
    assert outcome.new_chapters == []
    source = first(novel)
    assert source.baseline_set is True
    assert source.latest_title.startswith("第60章")
    assert len(source.notified_keys) == 60


def test_new_chapter_reports_updated_with_its_title():
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, None)

    updated = fixtures.normal_html([*fixtures.CHAPTERS, "第61章 新的开始"])
    fetcher.add(fixtures.BASE_URL, updated)
    outcome = check_source(novel, first(novel), RULES, fetcher, None)
    assert outcome.status == STATUS_UPDATED
    assert [c.title for c in outcome.new_chapters] == ["第61章 新的开始"]
    assert outcome.latest.title == "第61章 新的开始"


def test_same_update_is_not_reported_twice():
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, None)
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 新的开始"]))
    assert check_source(novel, first(novel), RULES, fetcher, None).status == STATUS_UPDATED
    assert check_source(novel, first(novel), RULES, fetcher, None).status == STATUS_NO_CHANGE


def test_multiple_new_chapters_are_all_listed():
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, None)
    added = ["第61章 风", "第62章 花", "第63章 雪", "第64章 月"]
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, *added]))
    outcome = check_source(novel, first(novel), RULES, fetcher, None)
    assert [c.title for c in outcome.new_chapters] == added
    assert "、".join(added) in outcome.summary()
    assert "更新 4 章" in outcome.summary()


def test_display_cap_limits_listed_titles_but_notifies_all_once():
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, None)
    added = [f"第{61 + i}章 大量更新{i}" for i in range(15)]
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, *added]))
    outcome = check_source(novel, first(novel), RULES, fetcher, None)
    assert len(outcome.new_chapters) == 10  # 弹窗只列最后 10 条
    assert outcome.new_chapters[-1] is not None and outcome.new_chapters[-1].title == "第75章 大量更新14"
    assert "更新 15 章" in outcome.summary()

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, *added, "第76章 收尾"]))
    again = check_source(novel, first(novel), RULES, fetcher, None)
    assert [c.title for c in again.new_chapters] == ["第76章 收尾"]


def test_chapter_renamed_without_new_number_is_not_an_update():
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, None)
    renamed = [t if i != 59 else "第60章 作者改了标题" for i, t in enumerate(fixtures.CHAPTERS)]
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html(renamed))
    assert check_source(novel, first(novel), RULES, fetcher, None).status == STATUS_NO_CHANGE


def test_site_renumbering_is_recorded_as_toc_reset_without_popup():
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, None)
    # 整站重编号：所有 href 变了，一次冒出 60 个新标识
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html(fixtures.CHAPTERS).replace('href="', 'href="/new/'))
    outcome = check_source(novel, first(novel), RULES, fetcher, None)
    assert outcome.status == STATUS_TOC_RESET
    assert outcome.new_chapters == []
    assert "重编号" in outcome.message or "只剩" in outcome.message


def test_network_error_keeps_previous_state():
    novel = make_novel()
    check_source(novel, first(novel), RULES,
                 FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}), None)
    source = first(novel)
    before = (source.latest_title, source.notified_keys.copy())

    class DeadFetcher:
        def fetch_html(self, url, proxy=""):
            raise FetchError("Connection timeout")

    outcome = check_source(novel, source, RULES, DeadFetcher(), None)
    assert outcome.status == STATUS_NET_ERROR
    assert source.latest_title == before[0]
    assert source.notified_keys == before[1]
    assert source.consecutive_failures == 1


def test_parse_failure_writes_candidates_file(paths):
    novel = make_novel()
    source = first(novel)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.no_container_html()})
    outcome = check_source(novel, source, RULES, fetcher, paths)
    assert outcome.status == "parse_failed"
    file = paths.source_candidates(source.id)
    assert file.exists()
    text = file.read_text(encoding="utf-8")
    assert "候选" in text and "selector" in text


def test_manual_selector_overrides_auto_detection(paths):
    novel = make_novel()
    source = first(novel)
    source.selector = "dl.list"
    html = fixtures.no_container_html().replace(
        "</body>",
        '<dl class="list">' + fixtures.toc_links([f"第{i}章 手写章节" for i in range(1, 25)]) + "</dl></body>",
    )
    outcome = check_source(novel, source, RULES, FakeFetcher({fixtures.BASE_URL: html}), paths)
    assert outcome.status == STATUS_BASELINE
    assert outcome.latest.number == 24


def test_keep_keys_restores_filtered_ad_like_link(paths):
    novel = make_novel()
    source = first(novel)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.ad_heavy_html()})
    check_source(novel, source, RULES, fetcher, paths)
    fake_key = normalize_chapter_url("https://book.example.com/xiaoshuo/9999/200.html")
    assert fake_key not in source.notified_keys

    source.keep_keys = [fake_key]
    outcome = check_source(novel, source, RULES, fetcher, paths)
    assert any(c.number == 200 for c in outcome.new_chapters)
    assert fake_key in source.notified_keys


def test_source_proxy_is_handed_to_the_fetcher():
    novel = make_novel()
    source = first(novel)
    source.proxy = "http://127.0.0.1:7890"
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, source, RULES, fetcher, None)
    assert fetcher.proxies == ["http://127.0.0.1:7890"]


def test_proxy_failure_has_its_own_status_and_keeps_state():
    novel = make_novel()
    source = first(novel)
    source.proxy = "socks5://127.0.0.1:7890"
    check_source(novel, source, RULES, FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}), None)
    before = (source.latest_title, source.notified_keys.copy())

    class DeadProxyFetcher:
        def fetch_html(self, url, proxy=""):
            raise ProxyError(f"{proxy} 走不通：[WinError 10061] 由于目标计算机积极拒绝")

    outcome = check_source(novel, source, RULES, DeadProxyFetcher(), None)
    assert outcome.status == STATUS_PROXY_ERROR
    assert status_label(outcome.status) == "代理不可用"
    assert "socks5://127.0.0.1:7890" in outcome.message
    assert (source.latest_title, source.notified_keys) == before
    assert source.consecutive_failures == 1


def test_drop_keys_removes_leaked_ad_chapter(paths):
    novel = make_novel()
    source = first(novel)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.ad_heavy_html()})
    check_source(novel, source, RULES, fetcher, paths)
    fake_key = normalize_chapter_url("https://book.example.com/xiaoshuo/9999/200.html")
    source.keep_keys = [fake_key]
    check_source(novel, source, RULES, fetcher, paths)
    assert source.chapter_count == 32

    source.keep_keys = []
    source.drop_keys = [fake_key]
    outcome = check_source(novel, source, RULES, fetcher, paths)
    assert outcome.source.chapter_count == 31
    assert fake_key not in [c.key for c in outcome.new_chapters]


def test_history_records_updates_with_titles(paths):
    novel = make_novel()
    source = first(novel)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, source, RULES, fetcher, paths)
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 记录我"]))
    check_source(novel, source, RULES, fetcher, paths)
    records = read_jsonl(paths.source_history(novel.id, source.id))
    update = [r for r in records if r["kind"] == "update"]
    assert update and update[0]["chapters"][0]["t"] == "第61章 记录我"
    assert update[0]["latest"]["u"].endswith("61.html")
    assert update[0]["site"] == "book.example.com"


def test_novel_title_is_filled_from_page(paths):
    novel = make_novel()
    check_source(novel, first(novel), RULES,
                 FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}), paths)
    assert first(novel).title == "测试这本书"
    assert novel.title == "测试这本书"


def test_each_source_dedups_on_its_own_urls(paths):
    """两个来源各自记自己的 notified_keys：同一个站点不会把另一站的章节当成已读。"""
    novel = make_novel()
    url_b = "https://other.example.com/12_3456/"
    other = Source(id=source_id_for(url_b), toc_url=url_b, site="other.example.com")
    novel.sources.append(other)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})

    first_outcome = check_source(novel, novel.sources[0], RULES, fetcher, paths)
    other_outcome = check_source(novel, other, RULES, fetcher, paths)
    assert first_outcome.status == other_outcome.status == STATUS_BASELINE
    assert novel.sources[0].notified_keys != other.notified_keys
    assert all(k.startswith("https://book.example.com") for k in novel.sources[0].notified_keys)
    assert all(k.startswith("https://other.example.com") for k in other.notified_keys)
    assert novel.title == "测试这本书", "书名以最先抓到的为准，不被第二个来源改写"


# ---------- 小说级聚合 ----------

def test_collect_alerts_trims_index_and_alerted_keys(paths):
    """章节索引和提醒账本都有上限：长跑不能把 novels.json 越写越大。"""
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, paths)

    extra = [f"第{i}章 补更章节{i:04d}" for i in range(61, 73)]
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, *extra]))
    outcome = check_source(novel, first(novel), RULES, fetcher, paths)
    assert outcome.status == STATUS_UPDATED

    alert = collect_alerts([novel], [outcome], keep=3, index_rows=5)[0]
    assert alert.count == 12, "一次补 12 章也要算全，不能被上限截掉计数"
    assert [e.n for e in novel.chapter_index] == [72, 71, 70, 69, 68], "卡片只留最近几章，新在前"
    assert len(novel.alerted_keys) == 3


def test_novel_alert_title_names_the_reporting_source(paths):
    """镜像站先更就说镜像站，并且点明主源还没更；主源随后补上同一章时不再重复提醒。"""
    novel = make_novel()
    url_b = "https://mirror.example.com/12_3456/"
    mirror = Source(id=source_id_for(url_b), toc_url=url_b, site="mirror.example.com")
    novel.sources.append(mirror)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                           url_b: fixtures.normal_html(list(fixtures.CHAPTERS))})
    sources = novel.sources
    assert all(o.status == STATUS_BASELINE for o in
               (check_source(novel, s, RULES, fetcher, paths) for s in sources))

    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 镜像先来"]))
    alert = collect_alerts([novel], [check_source(novel, s, RULES, fetcher, paths) for s in sources])[0]
    assert alert.sites == ["mirror.example.com"]
    assert "来自 mirror.example.com，主源未更新" in alert.title()

    fetcher.add(fixtures.BASE_URL,
                fixtures.normal_html([*fixtures.CHAPTERS, "第61章 镜像先来", "第62章 主源补上"]))
    alert = collect_alerts([novel], [check_source(novel, s, RULES, fetcher, paths) for s in sources])[0]
    assert alert.count == 1, "第61章镜像站报过了，主源再报同一章不算新提醒"
    assert alert.sites == ["book.example.com"]
    assert "主源未更新" not in alert.title()


def test_collect_alerts_merges_sources_into_one_alert_per_novel(paths):
    """两个来源同一轮各自更新也只出一张卡；书架上有两本书就是两张卡。"""
    novel = make_novel()
    url_b = "https://mirror.example.com/12_3456/"
    mirror = Source(id=source_id_for(url_b), toc_url=url_b, site="mirror.example.com")
    novel.sources.append(mirror)
    other = make_novel("https://other.example.com/9_9/")

    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                           url_b: fixtures.normal_html(list(fixtures.CHAPTERS)),
                           other.sources[0].toc_url: fixtures.normal_html()})
    for source in novel.sources:
        check_source(novel, source, RULES, fetcher, paths)
    check_source(other, other.sources[0], RULES, fetcher, paths)

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 主源更的"]))
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 主源更的", "第62章 镜像更的"]))
    fetcher.add(other.sources[0].toc_url, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 另一本更的"]))
    outcomes = [check_source(novel, source, RULES, fetcher, paths) for source in novel.sources]
    outcomes.append(check_source(other, other.sources[0], RULES, fetcher, paths))

    alerts = collect_alerts([novel, other], outcomes)
    assert len(alerts) == 2, "卡片按小说张数，不按来源张数"
    merged = alerts[0]
    assert [e.n for e in merged.entries] == [62, 61], "跨来源合并后仍按章节号新在前"
    assert [e.s for e in merged.entries] == [mirror.id, novel.sources[0].id], "谁先报这一章就归谁"
    assert set(merged.sites) == {"mirror.example.com", "book.example.com"}
    assert alerts[1].novel is other and alerts[1].count == 1


def test_baseline_of_a_late_source_does_not_alert(paths):
    """主源更过第61章之后才挂上来的慢镜像，基线里那批旧章不能当更新再弹一遍。"""
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    check_source(novel, first(novel), RULES, fetcher, paths)
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 主源先更"]))
    collect_alerts([novel], [check_source(novel, first(novel), RULES, fetcher, paths)])

    url_b = "https://mirror.example.com/12_3456/"
    mirror = Source(id=source_id_for(url_b), toc_url=url_b, site="mirror.example.com")
    novel.sources.append(mirror)
    late = fixtures.normal_html([*fixtures.CHAPTERS, "第61章 主源先更"])
    fetcher.add(url_b, late)
    assert collect_alerts([novel], [check_source(novel, mirror, RULES, fetcher, paths)]) == []

    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 主源先更", "第62章 慢镜像补上"]))
    alert = collect_alerts([novel], [check_source(novel, mirror, RULES, fetcher, paths)])[0]
    assert alert.count == 1
    assert alert.entries[0].t == "第62章 慢镜像补上"


def test_same_host_sources_still_know_which_one_reported(paths):
    """两个来源同域不同路径（本地测试站常见）：主源没更时「主源未更新」照样要说。"""
    novel = make_novel()
    url_b = "https://book.example.com/99_99/"
    same_host = Source(id=source_id_for(url_b), toc_url=url_b, site="book.example.com")
    novel.sources.append(same_host)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                           url_b: fixtures.normal_html(list(fixtures.CHAPTERS))})
    for source in novel.sources:
        check_source(novel, source, RULES, fetcher, paths)

    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 只有第二来源更了"]))
    alert = collect_alerts([novel], [check_source(novel, s, RULES, fetcher, paths)
                                     for s in novel.sources])[0]
    assert alert.sites == ["book.example.com"], "站点名一样也分得清是谁报的"
    assert "主源未更新" in alert.title()
    assert alert.entries[0].s == same_host.id


# ---------- 分页与「最新一章」的取法 ----------

SECOND_PAGE = "https://book.example.com/12_3456/p-2.html"


def test_only_a_next_page_link_still_reaches_the_newest_chapter():
    """站点只挂「下一页」：不跟这一页就永远看不到第二页的最新章节。"""
    later = [*fixtures.CHAPTERS, *[f"第{i}章 第二页{i:04d}" for i in range(61, 71)]]
    res = load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.next_only_html(),
                                SECOND_PAGE: fixtures.next_only_html(later, nav="")}),
                   fixtures.BASE_URL, RULES)
    assert res.toc.pages_followed == 2
    assert len(res.toc.chapters) == 70
    assert res.toc.latest.title == "第70章 第二页0070"


def test_a_next_link_elsewhere_on_the_site_is_not_followed():
    """/module.php?go=next 这类假翻页不跟：白敲一次站点一次门，还可能触发风控。"""
    res = load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}), fixtures.BASE_URL, RULES)
    assert res.toc.pages_followed == 1


def test_a_tail_block_with_restarted_numbering_is_still_the_newest():
    """站点把编号写乱（正文排到 200 章，末尾另挂一块 181~187）：认目录顺序，不猜编号。"""
    titles = [f"第{i}章 正文{i:04d}" for i in range(1, 201)] + [f"第{i}章 另挂{i:04d}" for i in range(181, 188)]
    res = load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(titles)}), fixtures.BASE_URL, RULES)
    assert res.toc.latest.title == "第187章 另挂0187"
    assert [c.title for c in res.toc.chapters[-2:]] == ["第186章 另挂0186", "第187章 另挂0187"]


def test_newest_first_page_with_one_stray_high_number():
    """倒序页里混进一条大编号：整体在倒着排就取开头那条。"""
    titles = [f"第{i}章 最新{i:04d}" for i in range(190, 180, -1)] + ["第1562章 混进来的"]
    res = load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(titles)}), fixtures.BASE_URL, RULES)
    assert res.toc.latest.title == "第190章 最新0190"


def test_following_an_extra_page_is_not_read_as_renumbering(paths):
    """刚开始多跟到一页时冒出的几十章是真新章，不能判成「疑似重编号」从此不提醒。"""
    novel = make_novel()
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    assert check_source(novel, first(novel), RULES, fetcher, paths).status == STATUS_BASELINE

    later = [*fixtures.CHAPTERS, *[f"第{i}章 第二页{i:04d}" for i in range(61, 101)]]
    fetcher.add(fixtures.BASE_URL, fixtures.next_only_html())
    fetcher.add(SECOND_PAGE, fixtures.next_only_html(later, nav=""))
    outcome = check_source(novel, first(novel), RULES, fetcher, paths)
    assert outcome.status == STATUS_UPDATED, RULES.cfg.max_report_new_chapters
    assert outcome.new_count == 40
    assert outcome.latest.title == "第100章 第二页0100"
    assert first(novel).pages_followed == 2


def test_big_toc_keeps_every_chapter_still_listed(paths):
    """目录比 notified_keys_keep 还大时不能忘掉在目录里的章节，否则每轮把老章当新章节。"""
    rules = Config(notified_keys_keep=50).compiled()
    novel = make_novel()
    many = [f"第{i}章 长书{i:04d}" for i in range(1, 121)]
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(many)})
    assert check_source(novel, first(novel), rules, fetcher, paths).status == STATUS_BASELINE
    assert len(first(novel).notified_keys) == 120

    second = check_source(novel, first(novel), rules, fetcher, paths)
    assert second.status == STATUS_NO_CHANGE, "同一份目录不该第二轮就冒出 70 章更新"
