"""轮询检查、启动摘要、卡片内容、已读流转、调度节奏（AppState 主链路）。"""

from __future__ import annotations

import fixtures
import pytest
from conftest import FakeFetcher

from shunshu.align import match_key
from shunshu.app import AppState
from shunshu.models import STATUS_BASELINE, STATUS_NET_ERROR, STATUS_UPDATED
from shunshu.scheduler import Scheduler


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
    """同一轮里两本各更一章：必须各占一个槽位（index 0、1），不能叠在同一个位置。"""
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
    assert [index for _payload, index in cards] == [0, 1], "卡片按槽位纵向错开"
    assert all("更新 1 章" in payload["title"] for payload, _ in cards)
    assert {payload["chapters"][0]["t"] for payload, _ in cards} == {"第61章 A更新", "第61章 B更新"}


def test_notifier_slot_skips_a_slot_still_held_from_earlier_batch(app_state: AppState, monkeypatch):
    """12 秒内连来两批更新：第二批不能又从 0 号槽位开始把第一批没消失的卡片盖住。"""
    import time as time_mod

    from shunshu.notify import Notifier

    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(index) or True)
    notifier = Notifier(alerts_path=app_state.paths.alerts_log, enabled=True)
    # 手动占住 0 号槽（模拟上一批卡片还在屏幕上）
    notifier._slots = [time_mod.monotonic() + 100]
    notifier.show("第二批的卡", ["内容"])
    assert cards == [1], "0 号还没释放，新卡必须顺延到 1 号槽位"


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


def test_check_one_reuses_prefetched_html_without_fetching_again(app_state: AppState):
    """添加书源时代理探测抓回的首页 HTML 透传给首轮：目录页只准敲一次门。"""
    html = fixtures.normal_html()
    fetcher = FakeFetcher({})  # 不给任何页面：一旦真去抓就会抛 FetchError
    app_state.set_fetcher(fetcher)
    novel = app_state.add_novel(fixtures.BASE_URL)
    outcomes = app_state.check_one(novel.id, prefetch={novel.sources[0].id: html})
    assert [o.status for o in outcomes] == [STATUS_BASELINE], "预取 HTML 必须直接拿来解析建档"
    assert fetcher.calls == [], "透传了 HTML 就不许再请求一次目录页"


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


def test_no_novels_round_is_harmless(app_state: AppState):
    assert app_state.run_round() == []
    assert app_state.startup_lines([])[0].startswith("书架为空")


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


def test_run_forever_does_not_add_a_second_startup_round(app_state: AppState, monkeypatch):
    """启动轮由托盘显式跑过，run_forever 不能再补一轮：那等于每次开机把所有站点连敲两遍。"""
    rounds = []
    monkeypatch.setattr(app_state, "run_round", lambda **kwargs: rounds.append(1) or [])
    scheduler = Scheduler(app_state)
    scheduler._stop.set()
    scheduler.run_forever()
    assert rounds == [], "进循环前不该先跑一轮"
