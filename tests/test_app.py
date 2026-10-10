from __future__ import annotations

import json

import fixtures
import pytest
from conftest import FakeFetcher

from shunshu.align import match_key
from shunshu.app import AppState, DuplicateSource
from shunshu.cli import build_parser, main
from shunshu.models import ChapterEntry, STATUS_BASELINE, STATUS_NET_ERROR, STATUS_UPDATED
from shunshu.scheduler import Scheduler
from shunshu.store import Paths, source_id_for


@pytest.fixture
def app_state(tmp_path, monkeypatch) -> AppState:
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    state = AppState(Paths(tmp_path / "data"))
    state.reload()
    return state


def test_add_novel_persists_and_rejects_duplicates(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.save_novels()
    assert novel.id == source_id_for(fixtures.BASE_URL)
    assert [n.id for n in AppState(app_state.paths).novels] == [novel.id]
    with pytest.raises(DuplicateSource):
        app_state.add_novel(fixtures.BASE_URL)


def test_add_novel_rejects_non_http_url(app_state: AppState):
    with pytest.raises(ValueError):
        app_state.add_novel("javascript:alert(1)")


def test_novels_json_is_hand_editable_then_reloaded(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    app_state.save_novels()
    raw = json.loads(app_state.paths.novels.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    source = raw["novels"][0]["sources"][0]
    source["selector"] = "dl.list"
    source["keep_keys"] = ["https://book.example.com/9999/200.html"]
    source["proxy"] = "http://127.0.0.1:7890"
    app_state.paths.novels.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    app_state.reload()
    loaded = app_state.novels[0].sources[0]
    assert loaded.selector == "dl.list"
    assert loaded.keep_keys == ["https://book.example.com/9999/200.html"]
    assert loaded.proxy == "http://127.0.0.1:7890"


def test_missing_or_null_proxy_loads_as_no_proxy(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    app_state.save_novels()
    raw = json.loads(app_state.paths.novels.read_text(encoding="utf-8"))
    raw["novels"][0]["sources"][0]["proxy"] = None
    app_state.paths.novels.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    app_state.reload()
    assert app_state.novels[0].sources[0].proxy == ""


def test_run_round_updates_state_and_logs(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}))
    outcomes = app_state.run_round()
    assert outcomes[0].status == STATUS_BASELINE
    persisted = AppState(app_state.paths)
    assert persisted.novels[0].sources[0].latest_title.startswith("第60章")
    assert "首次记录" in app_state.paths.runtime_log.read_text(encoding="utf-8")


def test_startup_lines_report_last_chapter_when_no_update(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}))
    first = app_state.run_round()
    lines = app_state.startup_lines(first)
    assert "首次记录：最后一章 第60章" in lines[1]

    again = app_state.run_round()
    assert "无更新：最后一章 第60章" in app_state.startup_lines(again)[1]


def test_startup_lines_name_proxy_failure_separately(app_state: AppState):
    from shunshu.http import ProxyError

    class DeadProxyFetcher:
        def fetch_html(self, url, proxy=""):
            raise ProxyError(f"{proxy} 走不通：[WinError 10061] 由于目标计算机积极拒绝")

    novel = app_state.add_novel(fixtures.BASE_URL)
    novel.sources[0].proxy = "http://127.0.0.1:7890"
    app_state.set_fetcher(DeadProxyFetcher())
    line = app_state.startup_lines(app_state.run_round(notify=False))[1]
    assert "代理不可用" in line and "127.0.0.1:7890" in line
    assert line.count("代理不可用") == 1, "状态名不该念两遍"


def test_startup_lines_name_the_source_that_reported(app_state: AppState):
    url_b = "https://mirror-novel.net/12_3456/"
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(fetcher)
    lines = app_state.startup_lines(app_state.run_round(notify=False))
    assert lines[0] == "共监控 1 本 2 个来源"
    assert "book.example.com" in lines[1] and "mirror-novel.net" in lines[2]


def test_startup_lines_list_every_new_chapter_title(app_state: AppState):
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS,
                                                        "第61章 风起", "第62章 云涌", "第63章 终局"]))
    outcomes = app_state.run_round(notify=False)
    line = app_state.startup_lines(outcomes)[1]
    assert "有更新，3 章" in line
    assert all(name in line for name in ("第61章 风起", "第62章 云涌", "第63章 终局"))


def test_notify_many_uses_one_card_per_book(app_state: AppState, monkeypatch):
    from shunshu.notify import Notifier

    url_b = "https://book.example.com/88_99/"
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    app_state.add_novel(fixtures.BASE_URL)
    app_state.add_novel(url_b)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 A更新"]))
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 B更新"]))
    outcomes = app_state.run_round(notify=False)
    assert [o.status for o in outcomes] == [STATUS_UPDATED, STATUS_UPDATED]

    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append((payload, index)) or True)
    notifier = Notifier(alerts_path=app_state.paths.alerts_log, enabled=True)
    assert notifier.notify_many(app_state.last_alerts) is True

    assert len(cards) == 2, "多本更新要各自一张卡片"
    assert [index for _payload, index in cards] == [0, 1], "卡片按序号纵向错开"
    assert all("更新 1 章" in payload["title"] for payload, _ in cards)
    assert {payload["chapters"][0]["t"] for payload, _ in cards} == {"第61章 A更新", "第61章 B更新"}


def test_single_book_update_toast_lists_only_that_book(app_state: AppState):
    from shunshu.notify import Notifier

    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 单独"]))
    app_state.run_round(notify=False)
    Notifier(alerts_path=app_state.paths.alerts_log, enabled=False).notify_many(app_state.last_alerts)
    text = app_state.paths.alerts_log.read_text(encoding="utf-8")
    assert "测试这本书" in text and "第61章 单独" in text
    assert "本小说有更新" not in text


def test_adding_a_book_pops_card_with_latest_five(app_state: AppState, monkeypatch):
    """新加的一本要立刻弹最近 5 章：确认认对了书，也能顺手点开开始读。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}))
    novel = app_state.add_novel(fixtures.BASE_URL)
    outcomes = app_state.check_one(novel.id)

    assert [o.status for o in outcomes] == [STATUS_BASELINE]
    assert len(cards) == 1, "添加成功要有一张卡片"
    payload = cards[0]
    assert payload["title"] == "已添加《测试这本书》"
    assert [r["t"] for r in payload["chapters"]] == [
        "第60章 测试章节0060", "第59章 测试章节0059", "第58章 测试章节0058",
        "第57章 测试章节0057", "第56章 测试章节0056",
    ]
    assert all(r["read"] is False for r in payload["chapters"])
    assert payload["novel"] == novel.id, "卡片要带小说 id，点了才能记到这本书头上"


def test_failed_first_check_pops_no_card(app_state: AppState, monkeypatch):
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    app_state.set_fetcher(FakeFetcher({}))  # 全部 URL 报网络错误
    novel = app_state.add_novel(fixtures.BASE_URL)
    assert [o.status for o in app_state.check_one(novel.id)] == [STATUS_NET_ERROR]
    assert cards == []


def test_card_shows_five_latest_not_just_new(app_state: AppState, monkeypatch):
    """只更新 1 章时卡片也要凑满最近 5 章，才能看出哪几章漏读。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(fetcher)
    app_state.run_round()
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 唯一新章"]))
    app_state.run_round()

    assert [r["t"] for r in cards[-1]["chapters"]] == [
        "第61章 唯一新章", "第60章 测试章节0060", "第59章 测试章节0059",
        "第58章 测试章节0058", "第57章 测试章节0057",
    ]
    assert [r["read"] for r in cards[-1]["chapters"]] == [False] * 5


def test_card_click_flows_through_read_log_into_next_card(app_state: AppState, monkeypatch):
    """卡片点开 → data/read.jsonl → 下一轮吸收进书架 → 那张卡片里这章变灰。"""
    from shunshu.store import append_jsonl

    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    key62 = match_key(62, "第62章 刚点开")
    append_jsonl(app_state.paths.read_log, {"ts": "2026-10-10 09:00:00", "novel": novel.id,
                                            "key": key62, "url": fixtures.BASE_URL + "62.html"})
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html(
        [*fixtures.CHAPTERS, "第61章 读过了", "第62章 刚点开", "第63章 还没看"]))
    app_state.run_round()

    rows = cards[-1]["chapters"]
    assert [r["read"] for r in rows] == [False, True, False, False, False]
    assert novel.read_keys == [key62]
    assert AppState(app_state.paths).novels[0].read_keys == novel.read_keys
    assert app_state.paths.read_log.exists() is False, "吸收完要把 read.jsonl 取走，别重复计数"

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html(
        [*fixtures.CHAPTERS, "第61章 读过了", "第62章 刚点开", "第63章 还没看", "第64章 又更新"]))
    app_state.run_round()
    assert novel.read_keys == [key62], "吸收过的记录不能重复追加"
    assert [r["read"] for r in cards[-1]["chapters"]] == [False, False, True, False, False]


def test_read_state_is_shared_across_sources(app_state: AppState, monkeypatch):
    """已读记在小说级：繁体站那边读过「第61章」，简体站这张卡片里同一章也是灰的。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    url_b = "https://mirror-novel.net/12_3456/"
    simple = [*fixtures.CHAPTERS, "第61章 风雪夜归人"]
    traditional = [f"第{i}章 測試章節{i:04d}" for i in range(1, 61)] + ["第61章 風雪夜歸人"]
    fetcher = FakeFetcher({
        fixtures.BASE_URL: fixtures.normal_html(simple),
        url_b: fixtures.normal_html(traditional),
    })
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    chapter = novel.sources[0].latest_chapter()
    assert app_state.mark_read(novel.id, chapter) is True
    assert app_state.mark_read(novel.id, chapter) is False, "同一章不重复记"

    fetcher.add(url_b, fixtures.normal_html([*traditional, "第62章 第二來源"]))
    app_state.run_round()
    rows = cards[-1]["chapters"]
    assert [r["t"] for r in rows[:3]] == ["第62章 第二来源", "第61章 风雪夜归人", "第60章 测试章节0060"]
    assert [r["read"] for r in rows[:2]] == [False, True], "繁简两份标题要认成同一章"


# ---------- 多来源提醒去重 ----------

def test_same_chapter_from_two_sources_pops_one_card(app_state: AppState, monkeypatch):
    """镜像站先更「第61章 風雪夜歸人」，主源随后更同一章：只提醒一次，卡片写明是谁报的。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.add_source(novel.id, url_b)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 風雪夜歸人"]))
    app_state.run_round()
    assert len(cards) == 1, "两个来源共 61 章这一轮只有一张卡片"
    row = cards[0]["chapters"][0]
    assert row["t"] == "第61章 风雪夜归人" and row["s"] == "mirror-novel.net"
    assert "更新 1 章" in cards[0]["title"] and "mirror-novel.net" in cards[0]["title"]
    assert "主源未更新" in cards[0]["title"], "更的不是主源，标题要说清楚"

    cards.clear()
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 风雪夜归人"]))
    app_state.run_round()
    assert cards == [], "同一章已经提醒过，换来源再报不重复"


def test_two_sources_updating_different_chapters_share_one_card(app_state: AppState, monkeypatch):
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.add_source(novel.id, url_b)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第62章 主源新章"]))
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第63章 镜像新章"]))
    app_state.run_round()

    assert len(cards) == 1, "一本书两个来源各更一章，也只要一张卡片"
    rows = cards[0]["chapters"]
    assert [r["t"] for r in rows[:2]] == ["第63章 镜像新章", "第62章 主源新章"]
    assert [r["s"] for r in rows[:2]] == ["mirror-novel.net", "book.example.com"]
    assert "更新 2 章" in cards[0]["title"]
    assert "来自 mirror-novel.net、book.example.com" in cards[0]["title"], "谁报了最新的章，谁排在前面"
    assert "主源未更新" not in cards[0]["title"]


def test_slow_mirror_baseline_blocks_its_old_chapters_from_alerting(app_state: AppState, monkeypatch):
    """主源已经到 60 章，慢镜像建基线时只有 40 章：它更到 41 章不该当成新章节刷屏。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.add_source(novel.id, url_b)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                           url_b: fixtures.normal_html(fixtures.CHAPTERS[:40])})
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(url_b, fixtures.normal_html(fixtures.CHAPTERS[:41]))
    app_state.run_round()
    assert cards == [], "主源基线里就有的章节，别的来源后来才看到也不算更新"

    cards.clear()
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS[:41], "第61章 镜像先更"]))
    app_state.run_round()
    assert len(cards) == 1 and cards[0]["chapters"][0]["t"] == "第61章 镜像先更"


def test_silent_round_still_records_the_alert(app_state: AppState, monkeypatch):
    """`--no-toast` 只是这一轮不弹，章节照样记账，下一轮不该再弹一遍。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 静默"]))
    app_state.run_round(notify=False)
    assert cards == []
    app_state.run_round()
    assert cards == [], "已经报过一轮的章节不能因为上一轮没弹卡片就再弹"


def test_alert_state_is_persisted_in_novels_json(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}))
    app_state.run_round(notify=False)
    fetcher = app_state._fetcher
    assert isinstance(fetcher, FakeFetcher)
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 落盘"]))
    app_state.run_round(notify=False)

    raw = json.loads(app_state.paths.novels.read_text(encoding="utf-8"))
    saved = raw["novels"][0]
    assert saved["alerted_keys"], "提醒要记账，重启后不重复弹"
    assert saved["chapter_index"][0]["t"] == "第61章 落盘"
    assert saved["chapter_index"][0]["s"] == novel.sources[0].id, "索引要记住最先报这一章的来源"

    reloaded = AppState(app_state.paths).novels[0]
    assert [e.t for e in reloaded.chapter_index][:1] == ["第61章 落盘"]
    assert isinstance(reloaded.chapter_index[0], ChapterEntry)


def test_tray_pending_counts_novels_not_sources(app_state: AppState):
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.add_source(novel.id, url_b)
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    same = fixtures.normal_html([*fixtures.CHAPTERS, "第61章 两边都更"])
    fetcher.add(fixtures.BASE_URL, same)
    fetcher.add(url_b, same)
    app_state.run_round(notify=False)

    app = TrayApp(app_state)
    assert len(app._pending_alerts()) == 1, "两个来源更同一章，待读只算一本"


def test_add_source_reuses_one_novel_and_keeps_both_sources(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    url_b = "https://other.example.com/12_3456/"
    source = app_state.add_source(novel.id, url_b)

    assert len(app_state.novels) == 1
    assert [s.toc_url for s in novel.sources] == [fixtures.BASE_URL, url_b]
    assert novel.primary_id == novel.sources[0].id, "主源默认是最先加的那个"
    assert novel.source(source.id) is source
    assert novel.source(url_b) is source, "目录页 URL 也能定位来源"
    assert app_state.novel(url_b) is novel, "用任一来源的 URL 都能找到这本书"


def test_add_source_rejects_url_already_on_another_book(app_state: AppState):
    other = app_state.add_novel("https://book.example.com/88_99/")
    novel = app_state.add_novel(fixtures.BASE_URL)
    with pytest.raises(DuplicateSource):
        app_state.add_source(novel.id, other.sources[0].toc_url)


def test_remove_last_source_drops_the_whole_novel(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    url_b = "https://other.example.com/12_3456/"
    second = app_state.add_source(novel.id, url_b)

    assert app_state.remove_source(novel.id, second.id) is second
    assert len(novel.sources) == 1
    assert app_state.novels[0].primary_id == novel.sources[0].id

    last = novel.sources[0]
    assert app_state.remove_source(novel.id, last.id) is last
    assert app_state.novels == []


def test_history_merges_records_from_every_source(app_state: AppState):
    url_b = "https://mirror-novel.net/12_3456/"
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    novel = app_state.add_novel(fixtures.BASE_URL)
    second = app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 甲站更"]))
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 乙站更"]))
    app_state.run_round(notify=False)

    records = app_state.history(novel.id)
    assert len(records) == 4, "两个来源各自的首轮和更新轮都要在这张清单里"
    assert {r["site"] for r in records} == {"book.example.com", "mirror-novel.net"}
    updates = [r for r in records if r["kind"] == "update"]
    assert {u["chapters"][0]["t"] for u in updates} == {"第61章 甲站更", "第61章 乙站更"}
    assert records == sorted(records, key=lambda r: r["ts"], reverse=True)
    assert app_state.paths.source_history(novel.id, second.id).exists()


# ---------- 轮询节奏 ----------

def test_fixed_interval_is_constant(app_state: AppState):
    """固定间隔模式：返回值应该完全一致，不再有抖动。"""
    for minutes in (5, 10, 30, 60):
        app_state.set_poll_minutes(minutes)
        scheduler = Scheduler(app_state)
        waits = [scheduler.next_wait_seconds() for _ in range(10)]
        expected = minutes * 60.0
        assert all(w == expected for w in waits), f"poll_minutes={minutes} 应返回 {expected} 秒"


def test_interval_supports_all_four_options(app_state: AppState):
    """托盘菜单里 5/10/30/60 四个选项都能正确设置和读取。"""
    for minutes in (5, 10, 30, 60):
        app_state.set_poll_minutes(minutes)
        assert app_state.cfg.poll_minutes == minutes
        scheduler = Scheduler(app_state)
        assert scheduler.next_wait_seconds() == minutes * 60.0


def test_repeated_failures_are_skipped_by_backoff(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(FakeFetcher({}))  # 全部 URL 报网络错误
    scheduler = Scheduler(app_state)
    for _ in range(app_state.cfg.failure_backoff_threshold):
        scheduler.run_once(force=True)
    source = novel.sources[0]
    assert source.consecutive_failures == app_state.cfg.failure_backoff_threshold
    assert source.status == STATUS_NET_ERROR
    skip = scheduler.due_skip_ids(__import__("time").time())
    assert source.id in skip
    outcomes = app_state.check_one(novel.id)  # 检查本身不受 skip 影响
    assert [o.status for o in outcomes] == [STATUS_NET_ERROR]


def test_success_clears_backoff(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    broken = FakeFetcher({})
    app_state.set_fetcher(broken)
    scheduler = Scheduler(app_state)
    for _ in range(3):
        scheduler.run_once(force=True)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}))
    outcome = scheduler.run_once(force=True)[0]
    assert outcome.source.consecutive_failures == 0
    assert scheduler.due_skip_ids(__import__("time").time()) == set()


def test_no_novels_round_is_harmless(app_state: AppState):
    assert app_state.run_round() == []
    assert app_state.startup_lines([])[0].startswith("书架为空")


# ---------- 入口 ----------

def test_cli_parser_defaults_to_run():
    args = build_parser().parse_args([])
    assert args.command is None


def test_cli_rejects_the_removed_subcommands(tmp_path, monkeypatch, capsys):
    """命令行不再对外提供操作：argparse 直接拒掉，也不去偷偷建数据目录。"""
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as exit_info:
        main(["list"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_popup_subcommand_still_accepts_what_notify_builds():
    """弹卡片是主程序内部调用：入口收窄后这条参数还得对得上。"""
    from shunshu.notify import popup_command
    from shunshu.popup import make_payload

    argv = popup_command(make_payload("标题", lines=["一行"]), 2, 12.0)[3:]
    args = build_parser().parse_args(argv)
    assert args.command == "popup" and args.index == 2 and args.seconds == 12.0
    assert json.loads(args.payload)["title"] == "标题"


def test_tray_source_line_shows_proxy_without_credentials(app_state: AppState):
    from shunshu.tray import TrayApp

    novel = app_state.add_novel(fixtures.BASE_URL)
    source = novel.sources[0]
    source.proxy = "socks5://reader:secretpwd@127.0.0.1:7890"
    app = TrayApp(app_state)

    labels = [str(item.text) for item in app._novel_submenu(novel.id)]
    assert any("来源 book.example.com" in t and "代理 socks5://127.0.0.1:7890" in t for t in labels)
    assert "secretpwd" not in " ".join(labels)


# ---------- P5 来源维护 ----------

def test_source_maintenance_lives_on_app_state(app_state: AppState):
    """主源、暂停、删除都记在 AppState 上并落盘，托盘菜单只是它的一张皮。"""
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)

    assert app_state.set_primary(novel.id, mirror.id) is mirror
    assert novel.primary_id == mirror.id
    assert AppState(app_state.paths).novels[0].primary_id == mirror.id, "换主源要落盘"

    assert app_state.set_source_enabled(novel.id, mirror.id, False) is mirror
    assert mirror.enabled is False
    assert app_state.set_source_enabled(novel.id, mirror.id, True) is mirror
    assert mirror.enabled is True

    assert app_state.set_primary(novel.id, "没有这个来源") is None
    assert app_state.set_source_enabled(novel.id, "没有这个来源", True) is None

    assert app_state.remove_source(novel.id, mirror.id) is mirror
    assert novel.primary_id == novel.sources[0].id, "主源被删后要落到还在的来源上"
    assert app_state.remove_source(novel.id, novel.sources[0].id) is not None
    assert app_state.novels == [], "最后一个来源移走，整本一起下架"


def test_tray_primary_and_pause_actions_take_effect(app_state: AppState, monkeypatch):
    from shunshu.tray import TrayApp

    monkeypatch.setattr("shunshu.notify.spawn_card", lambda payload, index=0, seconds=12.0: True)
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app = TrayApp(app_state)

    app.act_set_primary(novel.id, mirror.id)
    assert novel.primary_id == mirror.id
    app.act_set_primary(novel.id, "没有这个来源")
    assert novel.primary_id == mirror.id, "找不到来源时不该把主源改坏"

    app.act_toggle_source(novel.id, mirror.id)
    assert mirror.enabled is False
    app.act_toggle_source(novel.id, mirror.id)
    assert mirror.enabled is True
    assert AppState(app_state.paths).novels[0].sources[1].enabled is True, "暂停状态也要落盘"


def test_tray_new_source_receipt_names_that_source(app_state: AppState, monkeypatch):
    """新来源首轮只建基线不弹卡，所以回执必须自己说清挂上了谁、抓到什么。"""
    from shunshu.tray import TrayApp

    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                                       url_b: fixtures.normal_html(list(fixtures.CHAPTERS))}))
    TrayApp(app_state)._check_new_source(novel.id, mirror.id)
    receipt = next(c for c in cards if "已挂上来源" in c["title"])
    assert "mirror-novel.net" in receipt["title"]
    assert "首次记录" in " ".join(receipt["lines"]), "首轮建基线要说清抓到什么，别让人以为没生效"


def test_tray_source_submenu_lists_actions(app_state: AppState):
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app = TrayApp(app_state)

    items = list(app._source_submenu(novel.id, mirror.id))
    labels = [str(i.text) for i in items]
    assert labels[0] == url_b, "第一行就是目录页网址，方便核对挂没挂对"
    assert "设为主源" in labels and "暂停此源" in labels and "删除此源" in labels
    assert next(i for i in items if str(i.text) == "设为主源").enabled is True

    primary_items = list(app._source_submenu(novel.id, novel.sources[0].id))
    assert next(i for i in primary_items if str(i.text) == "设为主源").enabled is False

    rows = [str(i.text) for i in app._novel_submenu(novel.id)]
    assert any(t.startswith("来源 mirror-novel.net") and "（主源）" not in t for t in rows)
    assert any(t.startswith("来源 book.example.com") and "（主源）" in t for t in rows)
    assert "添加来源到本书（从剪贴板）" in rows


def test_tray_delete_source_needs_two_clicks(app_state: AppState, monkeypatch):
    from shunshu.tray import TrayApp

    monkeypatch.setattr("shunshu.notify.spawn_card", lambda payload, index=0, seconds=12.0: True)
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app = TrayApp(app_state)

    app.act_delete_source(novel.id, mirror.id)
    assert len(novel.sources) == 2, "第一次点只是武装，不能真删"
    app.act_delete_source(novel.id, mirror.id)
    assert [s.id for s in novel.sources] == [novel.sources[0].id]


def test_tray_add_source_from_clipboard_then_baselines_quietly(app_state: AppState, monkeypatch):
    """剪贴板里是另一个站的目录页：挂上后首轮建基线，不弹「已添加」卡，只给一句回执。"""
    from shunshu import tray
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    monkeypatch.setattr(tray, "read_text", lambda: f"这个站更快 {url_b}")
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                                       url_b: fixtures.normal_html(list(fixtures.CHAPTERS))}))
    app = TrayApp(app_state)
    app._check_new_source = lambda novel_id, source_id: None

    app.act_add_source_from_clipboard(novel.id)
    assert [s.toc_url for s in novel.sources] == [fixtures.BASE_URL, url_b]
    assert cards == [], "追加来源不该再弹一张「已添加」卡"

    app.act_add_source_from_clipboard(novel.id)
    assert len(novel.sources) == 2, "同一网址不能挂两次"
    assert any("已在书架里" in str(c["lines"]) + c["title"] for c in cards)


def test_displayed_titles_are_simplified_everywhere(app_state: AppState):
    """显示统一简体：来源行与摘要行里都不能冒出繁体（网页本身仍是繁体，跳转地址不变）。"""
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    traditional = [f"第{i}章 測試章節{i:04d}" for i in range(1, 61)] + ["第61章 風雪夜歸人"]
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                                       url_b: fixtures.normal_html(traditional)}))
    outcomes = app_state.check_one(novel.id, notify=False)

    summaries = "".join(o.summary() for o in outcomes)
    assert "風雪" not in summaries and "測試" not in summaries
    assert "第61章 风雪夜归人" in summaries
    assert mirror.latest_title == "第61章 風雪夜歸人", "存下来的是源站原文，转简体只发生在显示那一步"
    rows = [str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, mirror.id)]
    assert any("最后一章《第61章 风雪夜归人》" in t for t in rows)
    assert mirror.toc_url == url_b


def test_tray_source_row_shows_which_proxy_actually_applies(app_state: AppState, monkeypatch):
    """代理只能在 json 里写，菜单这一行负责说清这一源实际走的是哪条路。"""
    from shunshu import tray
    from shunshu.tray import TrayApp

    novel = app_state.add_novel(fixtures.BASE_URL)
    source = novel.sources[0]
    monkeypatch.setattr(tray, "system_proxy_for", lambda url: "http://me:pw@127.0.0.1:7890")
    rows = " | ".join(str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, source.id))
    assert "代理 跟随系统 http://127.0.0.1:7890" in rows
    assert "pw" not in rows, "系统代理带账号也不显示凭据"

    source.proxy = "socks5://10.0.0.9:1080"
    rows = " | ".join(str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, source.id))
    assert "代理 socks5://10.0.0.9:1080（手写覆盖）" in rows

    source.proxy = ""
    monkeypatch.setattr(tray, "system_proxy_for", lambda url: "")
    rows = " | ".join(str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, source.id))
    assert "当前没有系统代理，直连" in rows
