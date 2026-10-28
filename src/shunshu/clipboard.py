"""读系统剪贴板：托盘「添加小说（从剪贴板）」用，不引入 GUI 依赖。

Windows 直接调 user32/kernel32；Linux 上没有 ctypes.windll，改为借道会话里的
命令行工具（Wayland 用 wl-paste，X11 用 xclip / xsel），代价是要装其中一个。
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess

from .host import IS_WINDOWS

_URL_RE = re.compile(r"https?://[^\s\"'<>\u3000-\u303f，。、]+", re.I)
_CF_UNICODETEXT = 13
_CLIPBOARD_TIMEOUT_SECONDS = 5.0
# (命令, 参数, 这条要求哪个会话变量存在)：命令没装就跳过，试下一个
_CLIPBOARD_TOOLS = (
    ("wl-paste", ("-n",), "WAYLAND_DISPLAY"),
    ("xclip", ("-selection", "clipboard", "-o"), "DISPLAY"),
    ("xsel", ("--clipboard", "--output"), "DISPLAY"),
)


def read_text() -> str | None:
    """返回剪贴板里的纯文本，拿不到就返回 None（不抛异常，菜单里要能降级提示）。"""
    return _read_windows() if IS_WINDOWS else _read_posix()


def _read_posix() -> str | None:
    for name, args, session_var in _CLIPBOARD_TOOLS:
        if not os.environ.get(session_var):
            continue
        try:
            done = subprocess.run([name, *args], capture_output=True,
                                  timeout=_CLIPBOARD_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout:
            return done.stdout.decode("utf-8", "replace")
    return None


def _read_windows() -> str | None:
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None

    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]

    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            size = kernel32.GlobalSize(handle)
            if size <= 1:
                return None
            text = ctypes.wstring_at(pointer, size // 2)
            return text.split("\x00", 1)[0]
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def extract_url(text: str | None) -> str | None:
    """从剪贴板内容里取第一个 http(s) 链接：浏览器复制的常是「标题 URL」或富文本。"""
    if not text:
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)]}》」』\"'")
