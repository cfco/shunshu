"""开机自启的启动延迟：代理客户端起得比托盘晚，启动那一轮要先等它。

只有写进自启配置的那条命令带 `--boot-delay`；手动启动（`启动.bat`、`uv run shunshu`）
不带，照旧当场检查。
"""

from __future__ import annotations

import shlex

import pytest

from conftest import capture_cards


# ---------- 自启命令里的开关 ----------

def test_windows_run_value_carries_the_delay(monkeypatch):
    from shunshu import autostart

    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "is_packaged", lambda: True)
    monkeypatch.setattr(autostart, "self_executable", lambda: r"C:\dist\shunshu.exe")
    assert autostart.current_command() == r'"C:\dist\shunshu.exe" run --boot-delay=60'


def test_source_mode_command_carries_the_delay(monkeypatch, tmp_path):
    from shunshu import autostart

    interpreter = tmp_path / "pythonw"
    interpreter.write_text("")
    monkeypatch.setattr(autostart, "IS_WINDOWS", False)
    monkeypatch.setattr(autostart, "is_packaged", lambda: False)
    monkeypatch.setattr(autostart.sys, "executable", str(interpreter))

    assert autostart.current_command() == (
        f"{shlex.quote(str(interpreter))} -m shunshu run --boot-delay=60")
    assert autostart.BOOT_DELAY_SECONDS == 60


# ---------- 命令行 ----------

def test_only_the_run_subcommand_takes_the_delay():
    from shunshu.cli import build_parser

    assert build_parser().parse_args(["run"]).boot_delay == 0
    # 自启命令写的是 `--boot-delay=60` 这一种写法（注册表/Exec 行里没有空格拆分）
    assert build_parser().parse_args(["run", "--boot-delay=60"]).boot_delay == 60
    assert build_parser().parse_args(["run", "--boot-delay", "60"]).boot_delay == 60
    assert not hasattr(build_parser().parse_args([]), "boot_delay")  # 裸 shunshu：没有这个开关


def test_run_passes_the_delay_to_the_tray(app_state, monkeypatch):
    from shunshu import cli

    seen: dict[str, object] = {}

    class FakeTray:
        def __init__(self, state, boot_delay=0):
            seen["boot_delay"] = boot_delay

        def run(self) -> None:
            pass

    monkeypatch.setattr("shunshu.tray.TrayApp", FakeTray)
    assert cli.main(["run", "--boot-delay", "60"]) == 0
    assert seen["boot_delay"] == 60
    assert cli.main([]) == 0
    assert seen["boot_delay"] == 0


# ---------- 启动那一轮真的等了 ----------

def _fake_scheduler(tray, monkeypatch, order: list[str], stop_requested: bool = False):
    monkeypatch.setattr(tray.scheduler, "wait_for_stop",
                        lambda seconds: order.append(f"wait:{int(seconds)}") or stop_requested)
    monkeypatch.setattr(tray.scheduler, "run_once", lambda **kwargs: order.append("round") or [])
    monkeypatch.setattr(tray.scheduler, "run_forever", lambda: order.append("loop"))


def test_boot_delay_defers_the_startup_round(app_state, monkeypatch):
    from shunshu.tray import TrayApp

    popped = capture_cards(monkeypatch)
    order: list[str] = []
    tray = TrayApp(app_state, boot_delay=60)
    _fake_scheduler(tray, monkeypatch, order)

    tray._startup_then_loop()

    assert order == ["wait:60", "round", "loop"]
    assert "启动检查" in popped[0][0]["title"]


def test_manual_start_does_not_wait(app_state, monkeypatch):
    from shunshu.tray import TrayApp

    capture_cards(monkeypatch)
    order: list[str] = []
    tray = TrayApp(app_state)
    _fake_scheduler(tray, monkeypatch, order)

    tray._startup_then_loop()

    assert order == ["round", "loop"]  # boot_delay=0：连等都不试


def test_quit_during_the_wait_skips_the_round_and_the_loop(app_state, monkeypatch):
    """等待期间点「退出」：不能再跑启动轮，否则等满 60 秒会给已经退出的托盘补一张卡。"""
    from shunshu.tray import TrayApp

    popped = capture_cards(monkeypatch)
    order: list[str] = []
    tray = TrayApp(app_state, boot_delay=60)
    _fake_scheduler(tray, monkeypatch, order, stop_requested=True)

    tray._startup_then_loop()

    assert order == ["wait:60"]
    assert popped == []


def test_wait_for_stop_wakes_immediately_on_quit(app_state):
    from shunshu.scheduler import Scheduler

    scheduler = Scheduler(app_state)
    assert scheduler.wait_for_stop(0.05) is False
    scheduler.stop()
    assert scheduler.wait_for_stop(60) is True
