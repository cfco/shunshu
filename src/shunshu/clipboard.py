"""读 Windows 剪贴板：托盘「添加小说（从剪贴板）」用，不引入 GUI 依赖。"""

from __future__ import annotations

import ctypes
import re

_URL_RE = re.compile(r"https?://[^\s\"'<>\u3000-\u303f，。、]+", re.I)
_CF_UNICODETEXT = 13


def read_text() -> str | None:
    """返回剪贴板里的纯文本，拿不到就返回 None（不抛异常，菜单里要能降级提示）。"""
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
