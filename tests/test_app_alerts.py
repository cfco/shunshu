"""多来源提醒去重：同一章跨源只报一次、一本书一张卡、提醒记账落盘。"""

from __future__ import annotations

import json

import fixtures

from shunshu.app import AppState
from conftest import FakeFetcher
from shunshu.models import ChapterEntry


def test_same_chapter_from_two_sources_pops_one_card(app_state: AppState, monkeypatch):
    """镜像站先更「第61章 風雪夜歸人」，主源随后更同一章：只提醒一次，卡片写明是谁报的。"""
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or "")
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
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or "")
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
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or "")
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


def test_read_keys_cap_zero_keeps_nothing(app_state: AppState):
    """notified_keys_keep=0 时 read_keys 必须保持为空：del [:-0] 等于 del [:0]，
    一条都删不掉会让已读列表无限增长。"""
    from shunshu.models import Chapter

    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.cfg.notified_keys_keep = 0
    assert app_state.mark_read(novel.id, Chapter(title="第1章", url="https://book.example.com/1/1.html", number=1))
    assert app_state.mark_read(novel.id, Chapter(title="第2章", url="https://book.example.com/1/2.html", number=2))
    reloaded = app_state.novel(novel.id)
    assert reloaded.read_keys == [], "上限为 0 时已读记录不能累积"
