"""开机自启：Windows 写 HKCU\\...\\Run 注册表项，Linux 写 XDG 的 autostart .desktop。

两套实现共用同一组对外函数（is_enabled / enable / disable / current_command /
repair_if_drifted），托盘菜单不关心自己跑在哪个平台上。

自启命令一律带上 `--boot-delay`：开机时代理客户端比托盘起得晚，启动那一轮立刻跑
会把走代理的来源全记成一次代理失败。手动启动（`启动.bat`、`uv run shunshu`）不带这个
开关，照旧当场检查。
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from .host import IS_WINDOWS
from .packaging import is_packaged, self_executable

RUN_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
VALUE_NAME = "shunshu"
BOOT_DELAY_SECONDS = 60
BOOT_DELAY_ARG = f"--boot-delay={BOOT_DELAY_SECONDS}"


def _command_parts() -> tuple[str, list[str]]:
    """（要跑的程序, 参数）。

    打包态必须指向真实存在的 exe（Nuitka 下是 sys.argv[0]，不是伪造的
    sys.executable\\python.exe），否则开机时静默拉起失败、自启形同虚设。
    源码态在 Windows 上用 pythonw.exe（不占控制台），Linux 上没有无控制台的
    解释器，就用当前解释器本身。
    """
    if is_packaged():
        return self_executable(), ["run", BOOT_DELAY_ARG]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return str(interpreter), ["-m", "shunshu", "run", BOOT_DELAY_ARG]


def current_command() -> str:
    """写进自启配置里的启动命令。"""
    exe, args = _command_parts()
    if IS_WINDOWS:
        return " ".join([f'"{exe}"', *args])
    return " ".join([shlex.quote(exe), *[shlex.quote(arg) for arg in args]])


def _entry_path(entry: str) -> Path:
    """从已写下的命令里取出「指的那个程序」，用来判断路径是不是漂移了。"""
    text = (entry or "").strip()
    if IS_WINDOWS:
        if text.startswith('"'):
            end = text.find('"', 1)
            if end > 0:
                return Path(text[1:end])
        return Path(text.split(" ")[0])
    try:
        parts = shlex.split(text)
    except ValueError:
        return Path("")  # Exec 里引号没闭合：当成漂移，重写一次就好
    return Path(parts[0]) if parts else Path("")


# ---------- Windows：注册表 Run 项 ----------

def _win_read() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return str(value)
    except OSError:
        return None


def _win_enable() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, current_command())
        return True
    except OSError:
        return False


def _win_disable() -> bool:
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


# ---------- Linux：~/.config/autostart/shunshu.desktop ----------

def autostart_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "autostart" / f"{VALUE_NAME}.desktop"


def _desktop_text(command: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={VALUE_NAME}\n"
        "Comment=novel toc update watcher\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _xdg_read() -> str | None:
    try:
        text = autostart_file().read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Exec="):
            return line[len("Exec="):].strip()
    return ""  # 文件在但没 Exec 行：仍算「开着」，交给 needs_repair 重写


def _xdg_write() -> bool:
    target = autostart_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_desktop_text(current_command()), encoding="utf-8")
        return True
    except OSError:
        return False


def _xdg_disable() -> bool:
    try:
        autostart_file().unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


# ---------- 对外 ----------

def read_entry() -> str | None:
    if IS_WINDOWS:
        return _win_read()
    return _xdg_read()


def is_enabled() -> bool:
    return read_entry() is not None


def needs_repair() -> bool:
    """自启里指的解释器/程序已不是当前这个（比如换了解释器或打包后搬了目录）。

    只比程序、不比参数：升级新加进命令的开关（`--boot-delay`）不会由这里补上，
    要在托盘里取消再勾一次「开机自启」。
    """
    entry = read_entry()
    if entry is None:
        return False
    try:
        return not _entry_path(entry).samefile(_entry_path(current_command()))
    except OSError:
        return True


def enable() -> bool:
    if IS_WINDOWS:
        return _win_enable()
    return _xdg_write()


def disable() -> bool:
    if IS_WINDOWS:
        return _win_disable()
    return _xdg_disable()


def repair_if_drifted() -> bool:
    if needs_repair():
        return enable()
    return False
