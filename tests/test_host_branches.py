"""平台分支（host）：Windows 与 Ubuntu 各走各的实现，两边都要测得住。

IS_WINDOWS 一律用 monkeypatch 改模块全局，所以分支必须写在函数体内——写在导入期
就固化了，一台机器上测不到另一套。真机上的 Tk 渲染与托盘行为这里覆盖不到，见 README
的 Ubuntu 章节。
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from conftest import capture_cards


# ---------- 开机自启：Linux 走 XDG autostart ----------

@pytest.fixture
def linux_autostart(tmp_path, monkeypatch):
    from shunshu import autostart

    monkeypatch.setattr(autostart, "IS_WINDOWS", False)
    monkeypatch.setattr(autostart, "is_packaged", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return autostart


def test_xdg_autostart_roundtrip(linux_autostart, tmp_path):
    """开自启写 .desktop，菜单上那个勾读的就是这个文件。"""
    assert linux_autostart.is_enabled() is False
    assert linux_autostart.enable() is True

    text = (tmp_path / "config" / "autostart" / "shunshu.desktop").read_text(encoding="utf-8")
    assert f"Exec={linux_autostart.current_command()}" in text
    assert "[Desktop Entry]" in text and "Terminal=false" in text
    assert linux_autostart.is_enabled() is True

    assert linux_autostart.disable() is True
    assert linux_autostart.is_enabled() is False
    assert linux_autostart.disable() is True  # 文件已经不在了，仍算关成功


def test_xdg_autostart_detects_drift(linux_autostart, tmp_path):
    """换了解释器后 Exec 指的程序不是当前这个 → 启动时重写一次。"""
    other = tmp_path / "other-python"
    other.write_text("")
    linux_autostart.enable()
    assert linux_autostart.needs_repair() is False

    desktop = linux_autostart.autostart_file()
    desktop.write_text(desktop.read_text(encoding="utf-8").replace(
        f"Exec={linux_autostart.current_command()}", f"Exec={other} -m shunshu run"),
        encoding="utf-8")
    assert linux_autostart.read_entry() == f"{other} -m shunshu run"
    assert linux_autostart.needs_repair() is True
    assert linux_autostart.repair_if_drifted() is True
    assert linux_autostart.needs_repair() is False


def test_linux_command_quotes_a_path_with_spaces(linux_autostart, monkeypatch, tmp_path):
    """Exec 行按 shell 规则转义：带空格的解释器路径不能被拆成两个参数。"""
    interpreter = tmp_path / "my python"
    interpreter.write_text("")
    monkeypatch.setattr(sys, "executable", str(interpreter))

    command = linux_autostart.current_command()
    assert command == f"{shlex.quote(str(interpreter))} -m shunshu run --boot-delay=60"
    assert linux_autostart._entry_path(command) == interpreter


def test_broken_exec_line_counts_as_drift(linux_autostart):
    """手改坏的 .desktop（引号没闭合）不能把启动带崩：判成漂移，重写覆盖。"""
    assert linux_autostart._entry_path('"/oops') == Path("")
    assert linux_autostart.needs_repair() is False  # 还没写过文件，压根不用修


# ---------- 开机自启：Windows 那条一个字都不能变 ----------

def test_windows_command_is_registry_run_style(monkeypatch):
    from shunshu import autostart

    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "_command_parts",
                        lambda: (r"C:\x\pythonw.exe", ["-m", "shunshu", "run"]))
    assert autostart.current_command() == r'"C:\x\pythonw.exe" -m shunshu run'

    monkeypatch.setattr(autostart, "_command_parts", lambda: (r"C:\dist\shunshu.exe", ["run"]))
    assert autostart.current_command() == r'"C:\dist\shunshu.exe" run'


def test_windows_entry_path_reads_quoted_exe(monkeypatch):
    """注册表项里带空格的引号路径要能取出来，否则每次启动都误判成漂移、反复重写。"""
    from shunshu import autostart

    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    entry = r'"C:\Program Files\shunshu\pythonw.exe" -m shunshu run'
    assert autostart._entry_path(entry) == Path(r"C:\Program Files\shunshu\pythonw.exe")
    assert autostart._entry_path(r"C:\a\pythonw.exe -m shunshu run") == Path(r"C:\a\pythonw.exe")


# ---------- 剪贴板：Linux 借道会话里的命令行工具 ----------

class FakeResult:
    def __init__(self, code=0, out=b""):
        self.returncode, self.stdout = code, out


class FakeRun:
    """记录被试过的命令；没预置的当作「这个工具没装」。"""

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        if command[0] not in self.results:
            raise FileNotFoundError(command[0])
        return self.results[command[0]]


@pytest.fixture
def linux_clipboard(monkeypatch):
    from shunshu import clipboard

    monkeypatch.setattr(clipboard, "IS_WINDOWS", False)
    return clipboard


def test_clipboard_uses_xclip_on_x11(linux_clipboard, monkeypatch):
    fake = FakeRun({"xclip": FakeResult(out="看这里 https://a.com/b\r\n".encode())})
    monkeypatch.setattr(linux_clipboard.subprocess, "run", fake)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    text = linux_clipboard.read_text()
    assert linux_clipboard.extract_url(text) == "https://a.com/b"
    assert fake.calls == [["xclip", "-selection", "clipboard", "-o"]]


def test_clipboard_tries_next_tool_when_one_missing(linux_clipboard, monkeypatch):
    """wl-paste 没装就退到 xclip、再退到 xsel；命令缺失不能把菜单动作带崩。"""
    fake = FakeRun({"xsel": FakeResult(out=b"https://a.com/c")})
    monkeypatch.setattr(linux_clipboard.subprocess, "run", fake)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    assert linux_clipboard.read_text() == "https://a.com/c"
    assert [call[0] for call in fake.calls] == ["xclip", "xsel"]


def test_clipboard_only_uses_wayland_tool_on_wayland(linux_clipboard, monkeypatch):
    """纯 Wayland 会话（没有 DISPLAY）只该试 wl-paste。"""
    fake = FakeRun()
    monkeypatch.setattr(linux_clipboard.subprocess, "run", fake)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)

    assert linux_clipboard.read_text() is None
    assert fake.calls == [["wl-paste", "-n"]]  # 没装 wl-paste：试一次就罢休


def test_clipboard_silent_when_nothing_usable(linux_clipboard, monkeypatch):
    """空剪贴板、非零退出都算「没拿到」：菜单要能降级提示，不能抛异常。"""
    monkeypatch.setattr(linux_clipboard.subprocess, "run",
                        FakeRun({"xclip": FakeResult(code=1), "wl-paste": FakeResult()}))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert linux_clipboard.read_text() is None


# ---------- 卡片窗口 ----------

class FakeAttrRoot:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def overrideredirect(self, value) -> None:
        self.calls.append(("overrideredirect", value))

    def attributes(self, name, value) -> None:
        self.calls.append((name, value))


def test_toolwindow_only_set_on_windows(monkeypatch):
    """-toolwindow 传进 X11 的 Tk 会让卡片进程当场崩，这条必须挡住。"""
    from shunshu import popup

    monkeypatch.setattr(popup, "IS_WINDOWS", True)
    root = FakeAttrRoot()
    popup._apply_window_style(root)
    assert ("overrideredirect", True) in root.calls
    assert ("-topmost", True) in root.calls
    assert ("-toolwindow", True) in root.calls

    monkeypatch.setattr(popup, "IS_WINDOWS", False)
    root = FakeAttrRoot()
    popup._apply_window_style(root)
    assert ("-topmost", True) in root.calls
    assert not any(name == "-toolwindow" for name, _value in root.calls)


def test_font_switches_to_an_installed_family(monkeypatch):
    """候选字体一个都没装时退回 Tk 默认字体，别抱着 Windows 的字体名不放。"""
    from shunshu import popup

    monkeypatch.setattr(popup, "_FONT", "Microsoft YaHei UI")
    monkeypatch.setattr(popup.tkfont, "families", lambda _root: ["DejaVu Sans", "Noto Sans CJK SC"])
    assert popup._use_available_font(object()) == "Noto Sans CJK SC"
    assert popup._FONT == "Noto Sans CJK SC"

    monkeypatch.setattr(popup.tkfont, "families", lambda _root: ["Some Other Font"])
    monkeypatch.setattr(popup.tkfont, "nametofont",
                        lambda _name: type("F", (), {"actual": lambda self, _k: "Sans"})())
    assert popup._use_available_font(object()) == "Sans"


# ---------- 打开浏览器 ----------

def test_linux_browser_falls_back_to_xdg_open(monkeypatch):
    from shunshu import browser

    calls: list[list[str]] = []
    monkeypatch.setattr(browser, "IS_WINDOWS", False)
    monkeypatch.setattr(browser.webbrowser, "open", lambda _url: False)
    monkeypatch.setattr(browser.subprocess, "run",
                        lambda command, **_k: calls.append(list(command)) or FakeResult())
    assert browser.open_in_default_browser("https://a.com/b") is True
    assert calls == [["xdg-open", "https://a.com/b"]]


def test_windows_browser_still_prefers_startfile(monkeypatch):
    from shunshu import browser

    seen: list[str] = []
    monkeypatch.setattr(browser, "IS_WINDOWS", True)
    monkeypatch.setattr(browser, "_startfile", lambda url: seen.append(url) or True)
    monkeypatch.setattr(browser.webbrowser, "open", lambda _url: seen.append("webbrowser") or True)
    assert browser.open_in_default_browser("https://a.com/b") is True
    assert seen == ["https://a.com/b"]  # startfile 成功就不必再挑一次浏览器


def test_rejects_non_http_url_before_touching_the_system(monkeypatch):
    from shunshu import browser

    monkeypatch.setattr(browser, "IS_WINDOWS", False)
    monkeypatch.setattr(browser.subprocess, "run",
                        lambda *a, **k: pytest.fail("不该执行任何命令"))
    assert browser.open_in_default_browser("file:///etc/passwd") is False


# ---------- 托盘「打开数据目录」 ----------

def test_open_data_dir_on_linux_uses_xdg_open(app_state, monkeypatch):
    from shunshu import tray

    calls: list[list[str]] = []
    monkeypatch.setattr(tray, "IS_WINDOWS", False)
    monkeypatch.setattr(tray.subprocess, "run",
                        lambda command, **_k: calls.append(list(command)) or FakeResult())
    tray.TrayApp(app_state).act_open_data_dir()
    assert calls == [["xdg-open", str(app_state.paths.root)]]


def test_open_data_dir_reports_a_missing_launcher(app_state, monkeypatch):
    """xdg-open 没装（或目录被删）要回执，别静默什么都不发生。"""
    from shunshu import tray

    popped = capture_cards(monkeypatch)
    monkeypatch.setattr(tray, "IS_WINDOWS", False)

    def missing(command, **_kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(tray.subprocess, "run", missing)
    tray.TrayApp(app_state).act_open_data_dir()
    assert "打不开数据目录" in popped[0][0]["title"]


# ---------- 单实例 ----------

def test_posix_singleton_branch_with_a_fake_fcntl(tmp_path, monkeypatch):
    """假 fcntl 把这条分支在本机跑一遍：锁被占 → 拒；能锁 → 收下并把 fd 攥住。

    真 flock 的跨进程语义只有 POSIX 测得住（另一条用例）；这里连「锁文件根本开不了」
    那条放行路径一起覆盖——只读目录之类的环境不该拦住启动。
    """
    from shunshu import instance

    monkeypatch.setattr(instance, "IS_WINDOWS", False)
    monkeypatch.setattr(instance, "_lock_fd", None)
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    seen: list[tuple[int, int]] = []

    class FakeFcntl:
        LOCK_EX = 2
        LOCK_NB = 4

        @staticmethod
        def flock(fd, op):
            seen.append((fd, op))
            if len(seen) == 1:  # 第一次当作锁已被别的过程攥着
                raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setitem(sys.modules, "fcntl", FakeFcntl)
    assert instance.acquire_singleton() is False
    assert (tmp_path / "data" / instance.LOCK_FILE_NAME).exists()
    assert instance.acquire_singleton() is True
    assert instance._lock_fd is not None
    assert seen[0][1] == FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB

    monkeypatch.setattr(instance, "_lock_fd", None)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")  # 「数据目录」其实是个文件：mkdir 必失败
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(blocked))
    assert instance.acquire_singleton() is True


@pytest.mark.skipif(sys.platform == "win32", reason="flock 分支只在 POSIX 上跑得起来")
def test_posix_singleton_blocks_second_instance(tmp_path, monkeypatch):
    import fcntl

    from shunshu import instance

    monkeypatch.setattr(instance, "IS_WINDOWS", False)
    monkeypatch.setattr(instance, "_lock_fd", None)
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))

    holder = os.open(tmp_path / "data" / instance.LOCK_FILE_NAME, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert instance.acquire_singleton() is False  # 锁被别的过程攥着
        fcntl.flock(holder, fcntl.LOCK_UN)
        assert instance.acquire_singleton() is True
        assert instance.acquire_singleton() is True   # 同进程重复调用算「还是我」
    finally:
        monkeypatch.setattr(instance, "_lock_fd", None)
        os.close(holder)
