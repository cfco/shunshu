"""用系统默认浏览器打开章节链接。"""

from __future__ import annotations

import subprocess
import webbrowser

from .host import IS_WINDOWS
from .http import assert_http_url

_BROWSER_TIMEOUT_SECONDS = 10.0


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


def _fallback_command(url: str) -> bool:
    command = (["rundll32.exe", "url.dll,FileProtocolHandler", url] if IS_WINDOWS
               else ["xdg-open", url])
    try:
        return subprocess.run(command, timeout=_BROWSER_TIMEOUT_SECONDS).returncode == 0
    except Exception:
        return False
