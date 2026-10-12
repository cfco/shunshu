"""开机自启：HKCU\\...\\Run 下写一个值，路径与本程序不符时自动纠正。"""

from __future__ import annotations

import sys
from pathlib import Path

from .packaging import is_packaged, self_executable

RUN_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
VALUE_NAME = "shunshu"


def current_command() -> str:
    """写进注册表 Run 项的启动命令。

    打包态必须指向真实存在的 exe（Nuitka 下是 sys.argv[0]，不是伪造的
    sys.executable\\python.exe），否则开机时静默拉起失败、自启形同虚设。
    """
    if is_packaged():
        return f'"{self_executable()}" run'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" -m shunshu run'


def _entry_path(entry: str) -> Path:
    text = (entry or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return Path(text[1:end])
    return Path(text.split(" ")[0])


def read_entry() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return str(value)
    except OSError:
        return None


def is_enabled() -> bool:
    return read_entry() is not None


def needs_repair() -> bool:
    """注册表里指的解释器/程序已不是当前这个（比如换了解释器或打包后搬了目录）。"""
    entry = read_entry()
    if entry is None:
        return False
    try:
        return not _entry_path(entry).samefile(_entry_path(current_command()))
    except OSError:
        return True


def enable() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, current_command())
        return True
    except OSError:
        return False


def disable() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def repair_if_drifted() -> bool:
    if needs_repair():
        return enable()
    return False
