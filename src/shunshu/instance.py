"""单实例闸门：托盘工具重复启动会多出几个图标。

Windows 用命名互斥体；Linux 没有 CreateMutexW，改用数据目录上一个文件的 flock。
两种锁都在进程退出时由系统回收，不需要（也没法）显式释放。
"""

from __future__ import annotations

import ctypes
import os

from .host import IS_WINDOWS

MUTEX_NAME = "Local\\shunshu-tray-single-instance"
LOCK_FILE_NAME = "shunshu.lock"
_ERROR_ALREADY_EXISTS = 183

_handle = None
_lock_fd = None


def acquire_singleton(name: str = MUTEX_NAME) -> bool:
    """拿到锁返回 True；已有实例在跑返回 False。拿不到句柄时放行，别拦住启动。"""
    return _acquire_windows(name) if IS_WINDOWS else _acquire_posix()


def _acquire_windows(name: str) -> bool:
    global _handle
    if _handle:
        return True
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # 不声明 restype 时 ctypes 默认按 int（32 位）返回，64 位系统上句柄高 32 位会被
    # 截掉：高地址句柄会变成 0/假值或与别的句柄撞车，单实例闸门时灵时不灵
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _handle = kernel32.CreateMutexW(None, False, name)
    if not _handle:
        _handle = None
        return True
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def _acquire_posix() -> bool:
    """数据目录里的 lock 文件加独占 flock；文件被别的过程占着就返回 False。"""
    global _lock_fd
    if _lock_fd is not None:
        return True
    import fcntl

    from .config import data_dir

    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(root / LOCK_FILE_NAME, os.O_CREAT | os.O_RDWR)
    except OSError:
        return True  # 连锁文件都开不了（只读目录等）：放行，与 Windows 拿不到句柄同口径
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd  # 必须一直攥着：fd 一关锁就没了
    return True
