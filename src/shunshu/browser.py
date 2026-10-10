"""用系统默认浏览器打开章节链接。"""

from __future__ import annotations

import subprocess
import webbrowser

from .http import assert_http_url


def open_in_default_browser(url: str) -> bool:
    """优先 os.startfile：它走 shell 关联，一定用系统默认浏览器；webbrowser 在 Windows 上有时会挑到别的应用。"""
    try:
        target = assert_http_url(url)
    except ValueError:
        return False
    try:
        import os

        if hasattr(os, "startfile"):
            os.startfile(target)  # type: ignore[attr-defined]
            return True
    except OSError:
        pass
    try:
        if webbrowser.open(target):
            return True
    except Exception:
        pass
    try:
        return subprocess.run(["rundll32.exe", "url.dll,FileProtocolHandler", target],
                              timeout=10).returncode == 0
    except Exception:
        return False
