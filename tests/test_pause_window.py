"""午休暂停检查：时段解析与计算、调度顺延、托盘开关与标签。"""

from __future__ import annotations

import json
from datetime import datetime

import fixtures
from conftest import FakeFetcher

from shunshu.app import AppState
from shunshu.config import Config, config_path, parse_hhmm
from shunshu.models import STATUS_UPDATED
from shunshu.scheduler import Scheduler


def at(hour: int, minute: int, second: int = 0) -> float:
    """本地时刻换成时间戳：时段算的是用户桌上那个钟，测试也要按本地时间喂。"""
    return datetime(2026, 10, 15, hour, minute, second).timestamp()


def lunch(start: str = "11:30", end: str = "13:30") -> Config:
    cfg = Config()
    cfg.pause_enabled = True
    cfg.pause_start = start
    cfg.pause_end = end
    return cfg


# ---------- 时段解析 ----------

def test_parse_hhmm_accepts_normal_and_loose_forms():
    assert parse_hhmm("11:30") == 11 * 60 + 30
    assert parse_hhmm("9:05") == 9 * 60 + 5
    assert parse_hhmm(" 13：30 ") == 13 * 60 + 30  # 全角冒号与首尾空格
    assert parse_hhmm("23:59") == 24 * 60 - 1


def test_parse_hhmm_rejects_garbage():
    for bad in ("", None, "11:60", "24:00", "abc", "1130", "11-30", "11:30:00", True):
        assert parse_hhmm(bad) is None, f"{bad!r} 不该被当成时间"


def test_window_needs_the_switch_on():
    cfg = Config()
    assert cfg.pause_window() is not None, "默认两端时间（11:30~13:30）本身是好的"
    assert cfg.pause_enabled is False
    assert cfg.pause_seconds_left(now=at(12, 0)) is None
    assert cfg.in_pause_window(now=at(12, 0)) is False


def test_window_is_none_when_times_are_broken():
    """开关开着但时间写歪：按「不暂停」处理，绝不能把轮询整个停掉还不吭声。"""
    for start, end in (("11:60", "13:30"), ("abc", "13:30"), ("", ""), ("11:30", "11:30")):
        cfg = lunch(start, end)
        assert cfg.pause_window() is None, f"{start!r}~{end!r} 应算不出时段"
        assert cfg.pause_seconds_left(now=at(12, 0)) is None


def test_same_start_and_end_is_not_a_full_day():
    """11:30~11:30 既像 0 小时也像 24 小时：按不设时段处理，否则程序会永远停抓。"""
    cfg = lunch("11:30", "11:30")
    assert cfg.in_pause_window(now=at(15, 0)) is False


# ---------- 时段内还剩多久 ----------

def test_lunch_window_bounds():
    cfg = lunch()
    assert cfg.pause_seconds_left(now=at(11, 29, 59)) is None
    inside = cfg.pause_seconds_left(now=at(12, 0))
    assert inside is not None and 5400 <= inside <= 5401, "12:00 该还剩约 90 分钟"
    assert cfg.pause_seconds_left(now=at(13, 29, 59)) is not None
    assert cfg.pause_seconds_left(now=at(13, 30)) is None
    assert cfg.pause_seconds_left(now=at(20, 0)) is None


def test_window_crossing_midnight():
    cfg = lunch("22:00", "06:30")
    assert cfg.in_pause_window(now=at(23, 0)) is True
    assert cfg.in_pause_window(now=at(3, 0)) is True
    assert cfg.in_pause_window(now=at(6, 29, 59)) is True
    assert cfg.in_pause_window(now=at(6, 30)) is False
    assert cfg.in_pause_window(now=at(21, 59, 59)) is False
    # 23:00 距 06:30 还有 7.5 小时
    assert 27000 <= cfg.pause_seconds_left(now=at(23, 0)) <= 27001


def test_seconds_left_never_below_one():
    """掐着结束那一分钟醒来可能还差半秒，至少睡 1 秒才让顺延的那一轮落在时段外。"""
    cfg = lunch("11:30", "13:30")
    assert cfg.pause_seconds_left(now=at(13, 29, 59)) >= 1.0


# ---------- 手改坏值的归一 ----------

def test_hand_edited_bool_and_time_values_are_normalized():
    assert Config.from_dict({"pause_enabled": "false"}).pause_enabled is False
    assert Config.from_dict({"pause_enabled": "on"}).pause_enabled is True
    assert Config.from_dict({"pause_enabled": "随便写"}).pause_enabled is False
    assert Config.from_dict({"pause_start": " 12:00 "}).pause_start == "12:00"
    assert Config.from_dict({"pause_start": 1200}).pause_window() is None
    # 老文件里根本没有这三个键：时间用内置默认值，但开关默认是关的，等于不暂停
    assert Config.from_dict({}).pause_enabled is False
    assert Config.from_dict({}).pause_seconds_left(now=at(12, 0)) is None


# ---------- 调度顺延 ----------

def drive_forever(app_state: AppState, monkeypatch, left_values: list, waits: list):
    """喂一条「时段内还剩几秒」的时间线跑主循环，返回真的跑过的轮数。"""

    def fake_left(self, now=None):
        return left_values.pop(0) if left_values else None

    monkeypatch.setattr(Config, "pause_seconds_left", fake_left)
    scheduler = Scheduler(app_state)
    rounds = []
    monkeypatch.setattr(scheduler, "run_once",
                        lambda *a, **kw: rounds.append(1) or scheduler._stop.set())
    monkeypatch.setattr(scheduler._wake, "wait", lambda timeout: waits.append(timeout) or False)
    scheduler.run_forever()
    return len(rounds)


def test_round_inside_pause_window_is_deferred(app_state: AppState, monkeypatch):
    """时段内的这一轮不抓：先睡到时段结束，醒来还在时段里就接着睡，出时段才跑第一轮。"""
    waits: list = []
    # 剩 5 分钟 → 醒来仍在时段内 → 再睡 → 这次出时段了 → 跑一轮，循环由 stop 收尾
    assert drive_forever(app_state, monkeypatch, [300.0, 300.0, 300.0, None], waits) == 1
    assert waits == [300.0, 300.0], "顺延期间睡的是「距时段结束的剩余秒数」，不是扫描间隔"


def test_pause_is_logged_once_per_window(app_state: AppState):
    """同一时段只留一行日志、叫一次托盘：查证据够了，不必每次循环都重复。"""
    called = []
    scheduler = Scheduler(app_state, on_pause=called.append)
    scheduler._announce_pause(300.0)
    scheduler._announce_pause(280.0)
    assert len(called) == 1
    log = app_state.paths.runtime_log.read_text(encoding="utf-8")
    assert "暂停时段 11:30~13:30 内不抓取" in log and log.count("内不抓取") == 1
    assert "约 5 分钟后" in log, "倒计时只进日志这一份（它说的是写下那行的当时）"


def test_announce_pause_cannot_kill_the_loop(app_state: AppState):
    """这句提示在主循环的 try 之外调用：托盘回调炸了也不能把轮询线程带走。"""

    def boom(text: str) -> None:
        raise RuntimeError("托盘炸了")

    scheduler = Scheduler(app_state, on_pause=boom)
    scheduler._announce_pause(300.0)  # 这行不抛异常就是本用例的全部要求
    assert "内不抓取" in app_state.paths.runtime_log.read_text(encoding="utf-8")


def test_updates_that_appear_during_the_window_are_reported_after(app_state: AppState):
    """顺延不能把更新吞掉：午休里冒出来的那一章，出时段的第一轮要照常报、照常弹。"""
    cfg = app_state.cfg
    cfg.pause_enabled = True  # 内置默认时段 11:30~13:30
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()})
    app_state.set_fetcher(fetcher)
    app_state.add_novel(fixtures.BASE_URL)
    app_state.run_round(notify=False)  # 上午那一轮建基线，最后一章是第60章

    # 站点在午休中间更了一章（12:20 的事，程序那时候根本没去敲门）
    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 午休更的章"]))

    fetched_at = []
    for stamp in (at(11, 35), at(12, 20), at(13, 0), at(13, 31)):
        if cfg.pause_seconds_left(now=stamp) is None:  # 主循环问的就是这句话
            before = len(fetcher.calls)
            outcomes = app_state.run_round(notify=False)
            if len(fetcher.calls) > before:
                fetched_at.append(stamp)
    assert fetched_at == [at(13, 31)], "时段内一轮都不该敲站点"
    assert outcomes[0].status == STATUS_UPDATED
    alert = app_state.last_alerts[0]
    assert alert.count == 1 and "第61章" in alert.latest.title


def test_disabled_pause_keeps_plain_interval_rounds(app_state: AppState, monkeypatch):
    waits: list = []
    interval = app_state.cfg.poll_minutes * 60.0
    assert drive_forever(app_state, monkeypatch, [None, None], waits) == 1
    assert waits == [interval], "没在时段内就该按扫描间隔睡、立刻跑这一轮"


def test_startup_and_manual_rounds_ignore_the_window(app_state: AppState, monkeypatch):
    """手动动作不拦：托盘点开的启动轮、添加书源那一下，人正盯着屏幕。"""
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html()}))
    app_state.add_novel(fixtures.BASE_URL)
    monkeypatch.setattr(app_state.cfg, "pause_enabled", True)
    monkeypatch.setattr(Config, "pause_seconds_left", lambda self, now=None: 600.0)
    scheduler = Scheduler(app_state)
    assert len(scheduler.run_once(force=True)) == 1
    assert len(app_state.run_round()) == 1


# ---------- 托盘 ----------

def test_pause_menu_label_shows_window_without_repeating_the_switch(app_state: AppState,
                                                                    monkeypatch):
    """开关状态由那个勾表达，标签不再跟着写「已关闭」。"""
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    assert app._pause_label() == "暂停检查（11:30~13:30）"
    app_state.cfg.pause_enabled = True
    monkeypatch.setattr(Config, "in_pause_window", lambda self, now=None: True)
    assert app._pause_label() == "暂停检查（11:30~13:30，暂停中）"
    monkeypatch.setattr(Config, "in_pause_window", lambda self, now=None: False)
    assert app._pause_label() == "暂停检查（11:30~13:30）"


def test_pause_menu_label_flags_broken_times_only_when_switched_on(app_state: AppState):
    """时间写歪：没勾上时照实写时段就行，勾上了才要说「无效」（开着也等于没暂停）。"""
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    app_state.cfg.pause_end = "13:70"
    assert app._pause_label() == "暂停检查（11:30~13:70）"
    app_state.cfg.pause_enabled = True
    assert "无效" in app._pause_label()


def test_act_toggle_pause_persists_and_reports(app_state: AppState, monkeypatch):
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    # 固定成「现在不在时段内」，否则测试在午休那两个小时里跑会走到另一句回执
    monkeypatch.setattr(Config, "pause_seconds_left", lambda self, now=None: None)
    cards = []
    monkeypatch.setattr(app, "_flash", lambda title, body: cards.append((title, body)))
    app.act_toggle_pause()
    assert app_state.cfg.pause_enabled is True
    saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["pause_enabled"] is True and saved["pause_start"] == "11:30"
    assert cards[0][0] == "已开启暂停检查"
    assert cards[0][1] == "11:30~13:30 整轮不抓，期间更新的章节由出时段的第一轮一起报出来"
    app.act_toggle_pause()
    assert json.loads(config_path().read_text(encoding="utf-8"))["pause_enabled"] is False
    assert "已关闭暂停检查" in cards[1][0]


def test_act_toggle_pause_says_when_times_are_invalid(app_state: AppState, monkeypatch):
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    app_state.cfg.pause_start = ""
    cards = []
    monkeypatch.setattr(app, "_flash", lambda title, body: cards.append((title, body)))
    app.act_toggle_pause()
    assert "时段没生效" in cards[0][0]
    assert "重新加载配置" in cards[0][1]


def test_turning_pause_off_inside_window_wakes_the_loop(app_state: AppState, monkeypatch):
    """时段中间关掉开关：那一觉本来睡到 13:30，得立刻叫醒它补一轮检查。"""
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    app_state.cfg.pause_enabled = True
    monkeypatch.setattr(Config, "in_pause_window", lambda self, now=None: True)
    woken = []
    monkeypatch.setattr(app.scheduler, "resume_now", lambda: woken.append(1))
    app.act_toggle_pause()
    assert woken == [1] and app_state.cfg.pause_enabled is False


def test_turning_pause_off_outside_window_leaves_sleep_alone(app_state: AppState, monkeypatch):
    """没在时段里就没有「睡过头」这件事，不该多敲一轮站点。"""
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    app_state.cfg.pause_enabled = True
    monkeypatch.setattr(Config, "in_pause_window", lambda self, now=None: False)
    woken = []
    monkeypatch.setattr(app.scheduler, "resume_now", lambda: woken.append(1))
    app.act_toggle_pause()
    assert woken == []


def test_enter_pause_updates_tooltip(app_state: AppState):
    from shunshu.tray import TrayApp

    app = TrayApp(app_state)
    app._enter_pause("暂停时段 11:30~13:30 内不抓取，13:30 恢复检查")
    assert "暂停时段" in app._tooltip()
