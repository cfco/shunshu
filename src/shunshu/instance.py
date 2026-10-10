"""单实例闸门：托盘工具重复启动会多出几个图标，用 Windows 命名互斥体挡住。

进程退出时句柄由系统回收，不需要（也没法）显式释放。
"""

from __future__ import annotations

import ctypes

MUTEX_NAME = "Local\\shunshu-tray-single-instance"
_ERROR_ALREADY_EXISTS = 183

_handle = None


def acquire_singleton(name: str = MUTEX_NAME) -> bool:
    """拿到锁返回 True；已有实例在跑返回 False。拿不到句柄时放行，别拦住启动。"""
    global _handle
    windll = getattr(ctypes, "windll", None)
    if windll is None:  # 非 Windows 上不做这个约束
        return True
    if _handle:
        return True
    kernel32 = windll.kernel32
    _handle = kernel32.CreateMutexW(None, False, name)
    if not _handle:
        _handle = None
        return True
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS
