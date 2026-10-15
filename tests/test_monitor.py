import fixtures
from conftest import FakeFetcher

from shunshu.config import Config
from shunshu.http import FetchError, ProxyError
from shunshu.models import (
    STATUS_BASELINE,
    STATUS_CHECK_ERROR,
    STATUS_NET_ERROR,
    STATUS_PROXY_ERROR,
    STATUS_NO_CHANGE,
    STATUS_TOC_RESET,
    STATUS_UPDATED,
    Chapter,
    Novel,
    Outcome,
    Source,
    normalize_chapter_url,
    status_label,
)
from shunshu.monitor import check_all, check_source, check_source_safe, collect_alerts
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


def test_last_page_fetch_failure_leaves_a_log_line(paths):
    """末页抓不到时留一行日志：光看结果分不清「站点本来就没分页」和「那一页抓挂了」。"""
    load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.paged_first_html()}),
             fixtures.BASE_URL, RULES, paths=paths)
    text = paths.runtime_log.read_text(encoding="utf-8")
    assert "末页跟进" in text and "没抓到" in text

    # 没传 paths 的老用法照常工作，只是不记日志
    assert toc({fixtures.BASE_URL: fixtures.paged_first_html()}).toc.pages_followed == 1


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


def test_keep_keys_restores_an_old_chapter_without_becoming_the_latest(paths):
    """捞回的是一章旧章时，不能因为它排在末尾就被当成「最新章节」。"""
    html = f"""<!DOCTYPE html><html><head><title>捞回旧章这本书</title></head>
<body><div class="listmain"><dl>
{fixtures.toc_links(fixtures.CHAPTERS[:30])}
  <DD class="ad"><A href="199.html">第5章 被广告容器误杀的旧章</A></DD>
</dl></div></body></html>"""
    novel = make_novel()
    source = first(novel)
    fetcher = FakeFetcher({fixtures.BASE_URL: html})
    check_source(novel, source, RULES, fetcher, paths)
    assert source.latest_number == 30  # 基线：目录里真正的最后一章

    source.keep_keys = [normalize_chapter_url("https://book.example.com/12_3456/199.html")]
    outcome = check_source(novel, source, RULES, fetcher, paths)

    assert any(c.number == 5 for c in outcome.new_chapters)  # 捞回的旧章要当新章报一次
    assert source.latest_number == 30, "捞回旧章不能顶掉最新章节"
    assert source.latest_title == "第30章 测试章节0030"


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


def test_alerted_ledger_keeps_the_newest_chapters_of_a_big_batch(paths):
    """补更的章数超过账本上限时，留下来的必须是最新那几章：将来追上来的是目录末尾那几章，
    不是开头那几章。留住旧的会让慢半拍的镜像把已经弹过卡的最新章节再报一遍。"""
    novel = make_novel()
    url_b = "https://mirror.example.com/12_3456/"
    mirror = Source(id=source_id_for(url_b), toc_url=url_b, site="mirror.example.com")
    novel.sources.append(mirror)
    batch = [f"第{i}章 补更大批量{i:04d}" for i in range(61, 69)]
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                           url_b: fixtures.normal_html(list(fixtures.CHAPTERS))})
    for source in novel.sources:
        check_source(novel, source, RULES, fetcher, paths)

    # 主源一次冒出 8 章，账本上限只有 5：被挤掉的必须是第 61~63 章（旧的）
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, *batch]))
    outcomes = [check_source(novel, s, RULES, fetcher, paths) for s in novel.sources]
    alert = collect_alerts([novel], outcomes, keep=5)[0]
    assert alert.count == 8
    assert sorted(int(k.split("|")[0]) for k in novel.alerted_keys) == [64, 65, 66, 67, 68]

    # 镜像现在才追上同一批：第 66~68 章上一轮已经弹过卡，不该再报
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, *batch]))
    outcomes = [check_source(novel, s, RULES, fetcher, paths) for s in novel.sources]
    alert = collect_alerts([novel], outcomes, keep=5)[0]
    assert [e.n for e in alert.entries] == [63, 62, 61], "只报账本里没有了的那几章旧章，新在前"


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


def test_first_check_of_a_multi_source_novel_indexes_every_source(paths):
    """新书一次挂两个来源、同轮各自首轮建档：两源的最近章节都要进索引。

    只收第一个来源的 recent 会让第二个来源的章节从索引里消失，之后某轮它们冒出来
    会被当成新章再报一遍。
    """
    novel = make_novel()
    url_b = "https://mirror.example.com/12_3456/"
    mirror = Source(id=source_id_for(url_b), toc_url=url_b, site="mirror.example.com")
    novel.sources.append(mirror)
    # 镜像只列最后 20 章、标题写法与主源不同（match_key 带标题，故各自成条）
    mirror_titles = [f"第{i}章 镜像{i:04d}" for i in range(41, 61)]
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                           url_b: fixtures.normal_html(mirror_titles)})
    outcomes = [check_source(novel, source, RULES, fetcher, paths) for source in novel.sources]
    assert [o.status for o in outcomes] == [STATUS_BASELINE, STATUS_BASELINE]

    alerts = collect_alerts([novel], outcomes)
    assert len(alerts) == 1 and alerts[0].added is True
    assert any(e.s == mirror.id for e in novel.chapter_index), "镜像首轮那几章也要进索引"
    assert any(e.s == novel.sources[0].id for e in novel.chapter_index), "主源的章节也在"


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


def test_unexpected_crash_is_isolated_to_one_source(paths):
    """没预料到的异常只能砸中它自己那个来源，整轮的记账与提醒必须照跑。

    这类异常（解析层崩、来源字段被手改成没法用的形状）不属于 check_source 认得的
    任何一类错误，以前会从工作线程逃到 f.result() 再重抛，_collect 就此不执行：
    那一轮已经写进来源级 notified_keys 的章节永远不会再提醒，托盘却毫无表示。
    """
    boom_url = "https://boom.example.com/1/"
    bad = make_novel(boom_url)
    good = make_novel("https://other.example.com/9/")

    class Crasher(FakeFetcher):
        def fetch_html(self, url: str, proxy: str = "") -> str:
            if url == boom_url:
                raise RuntimeError("解析层炸了")
            return super().fetch_html(url, proxy)

    fetcher = Crasher({good.sources[0].toc_url: fixtures.normal_html()})
    outcomes = check_all([bad, good], RULES, fetcher, paths)

    assert len(outcomes) == 2, "一个来源崩了不能带走另一个"
    crashed = outcomes[bad.sources[0].id]
    assert crashed.status == STATUS_CHECK_ERROR
    assert "RuntimeError" in crashed.message
    assert bad.sources[0].consecutive_failures == 1, "崩一次也算失败，要进退避"
    assert outcomes[good.sources[0].id].status == STATUS_BASELINE
    assert paths.runtime_log.read_text(encoding="utf-8").count("检查异常") == 1


def test_check_source_safe_covers_the_single_book_path(paths):
    """「添加书源首轮」走的是同一条兜底：意外异常不会把后台线程白死掉。"""
    novel = make_novel()

    class Crasher(FakeFetcher):
        def fetch_html(self, url: str, proxy: str = "") -> str:
            raise RuntimeError("解析层炸了")

    outcome = check_source_safe(novel, first(novel), RULES, Crasher({}), paths)
    assert outcome.status == STATUS_CHECK_ERROR
    assert status_label(STATUS_CHECK_ERROR) == "检查异常"
    assert "解析层炸了" in outcome.detail()


def test_collect_alerts_orders_across_sources_by_distance_from_end():
    """合卡首行取「离自己目录末尾最近」的那章，不是目录名次最大的那章。

    主源列全目录（第62章名次 999），镜像只列最近 20 章（第63章名次 19）：
    跨来源直接比名次会把旧一章排在前面，卡片首行就不是最新章。
    """
    novel = Novel(id="n1", title="书", primary_id="s1")
    main = Source(id="s1", toc_url="https://a.example/1/", site="a", chapter_count=1000)
    mirror = Source(id="s2", toc_url="https://b.example/1/", site="b", chapter_count=20)
    novel.sources = [main, mirror]
    c62 = Chapter(title="第62章 甲", url="https://a.example/1/62.html", number=62, position=999)
    c63 = Chapter(title="第63章 乙", url="https://b.example/1/63.html", number=63, position=19)
    outcomes = [Outcome(novel=novel, source=main, status=STATUS_UPDATED, all_new=[c62]),
                Outcome(novel=novel, source=mirror, status=STATUS_UPDATED, all_new=[c63])]

    alerts = collect_alerts([novel], outcomes)

    assert [e.t for e in novel.chapter_index] == ["第63章 乙", "第62章 甲"]
    assert [e.t for e in alerts[0].entries] == ["第63章 乙", "第62章 甲"]
    assert alerts[0].origin_text() == "来自 b、a"


def test_collect_alerts_same_relative_age_prefers_bigger_number():
    """两个来源各报到「自己目录末尾同样远」的一章时，按章节号分新旧。"""
    novel = Novel(id="n1", title="书", primary_id="s1")
    main = Source(id="s1", toc_url="https://a.example/1/", site="a", chapter_count=100)
    mirror = Source(id="s2", toc_url="https://b.example/1/", site="b", chapter_count=20)
    novel.sources = [main, mirror]
    late = Chapter(title="第63章 乙", url="https://a.example/1/63.html", number=63, position=99)
    early = Chapter(title="第62章 甲", url="https://b.example/1/62.html", number=62, position=19)

    collect_alerts([novel], [Outcome(novel=novel, source=main, status=STATUS_UPDATED, all_new=[late]),
                             Outcome(novel=novel, source=mirror, status=STATUS_UPDATED, all_new=[early])])

    assert [e.t for e in novel.chapter_index] == ["第63章 乙", "第62章 甲"]


def test_handwritten_selector_also_follows_last_page():
    """手写了 selector 也要跟末页：只敲首页的话，分页站的最新章节永远看不到。"""
    result = load_toc(FakeFetcher({
        fixtures.BASE_URL: fixtures.paged_first_html(),
        fixtures.BASE_URL + "_20.html": fixtures.paged_last_html(),
    }), fixtures.BASE_URL, RULES, selector="div.listmain dl")

    assert result.toc.pages_followed == 2
    assert result.toc.chapters[-1].title == "第六百章 大结局"
    assert result.pages == [fixtures.BASE_URL, fixtures.BASE_URL + "_20.html"]


def test_handwritten_selector_falls_back_when_second_page_differs():
    """第二页结构不同、selector 认不出时退回只用首页，别把整次检查判成解析失败。"""
    result = load_toc(FakeFetcher({
        fixtures.BASE_URL: fixtures.paged_first_html(),
        fixtures.BASE_URL + "_20.html": "<html><body><p>这一页没有目录</p></body></html>",
    }), fixtures.BASE_URL, RULES, selector="div.listmain dl")

    assert result.toc.pages_followed == 1
    assert len(result.toc.chapters) == 60
