"""用系统默认浏览器打开章节链接。"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import webbrowser

from .host import IS_WINDOWS
from .http import assert_http_url

_BROWSER_TIMEOUT_SECONDS = 10.0
_LAUNCH_GRACE_SECONDS = 0.5


def open_in_default_browser(url: str) -> bool:
    """优先 os.startfile：它走 shell 关联，一定用系统默认浏览器；webbrowser 在 Windows 上有时会挑到别的应用。

    Linux 上没有 startfile，顺序反过来：先 webbrowser（它会用 $BROWSER 或 xdg-open），
    再直接 xdg-open 兜一次。
    """
    try:
        target = assert_http_url(url)
    except ValueError:
        return False
    if IS_WINDOWS and _startfile(target):
        return True
    if not IS_WINDOWS:
        return _posix_browser(target)
    if _webbrowser(target):
        return True
    return _fallback_command(target)


def _startfile(url: str) -> bool:
    try:
        import os

        if hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]
            return True
    except OSError:
        pass
    return False


def _webbrowser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _reap(proc) -> None:
    try:
        proc.wait()
    except OSError:
        pass


def _launch(command: list[str]) -> bool:
    try:
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            return proc.wait(timeout=_LAUNCH_GRACE_SECONDS) == 0
        except subprocess.TimeoutExpired:
            threading.Thread(target=_reap, args=(proc,), daemon=True).start()
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def _posix_browser(url: str) -> bool:
    commands = []
    for entry in os.environ.get("BROWSER", "").split(os.pathsep):
        if not entry.strip():
            continue
        try:
            command = shlex.split(entry)
        except ValueError:
            continue
        if not command:
            continue
        if any("%s" in part for part in command):
            commands.append([part.replace("%s", url) for part in command])
        else:
            commands.append([*command, url])
    commands.append(["xdg-open", url])
    return any(_launch(command) for command in commands)


def _fallback_command(url: str) -> bool:
    command = (["rundll32.exe", "url.dll,FileProtocolHandler", url] if IS_WINDOWS
               else ["xdg-open", url])
    try:
        return subprocess.run(command, timeout=_BROWSER_TIMEOUT_SECONDS).returncode == 0
    except Exception:
        return False
