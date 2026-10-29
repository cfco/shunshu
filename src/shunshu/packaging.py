"""运行形态判定：源码态 / 打包态（Nuitka、PyInstaller）。

开机自启命令、data 目录定位、卡片子进程拉起都依赖同一套结论，统一放这里，
避免各处再去猜 sys.frozen——Nuitka 不设置它，只猜 sys.frozen 会把 exe 当成源码态。
"""

from __future__ import annotations

import os
import sys


def is_packaged() -> bool:
    """当前是否运行在打包出来的可执行文件里。

    PyInstaller 设 `sys.frozen`，直接认。
    Nuitka 两件事都不按常理来：`__compiled__` 只注入入口模块（子模块里查不到），
    还把 Windows 下的 `sys.executable` 伪造成产物目录下一个并不存在的 `python.exe`，
    真实启动路径在 `sys.argv[0]`。所以 Windows 态判「argv[0] 是真实文件而
    sys.executable 指向的解释器不存在」。

    Linux 上 Nuitka 更狡猾：`argv[0]` 和 `sys.executable` 都指向那个真实存在的产物
    文件，只看解释器存不存在会判成源码态——卡片子进程于是被拼成
    `./shunshu -m shunshu popup`（产物不认 `-m`，一张卡都弹不出来），`data/` 也改成从
    `__file__` 往上找 `pyproject.toml`，找不到就落到莫名目录（书架与代理记忆全丢）。
    所以先认入口模块的 `__compiled__`，那条启发式退回作 Windows 的兜底。
    """
    if getattr(sys, "frozen", False):
        return True
    if hasattr(sys.modules.get("__main__"), "__compiled__"):
        return True
    invoked = sys.argv[0] if sys.argv else ""
    return bool(invoked) and os.path.isfile(invoked) and not os.path.isfile(sys.executable or "")


def self_executable() -> str:
    """打包态下「再次拉起自己」该用的可执行文件绝对路径。

    Nuitka onefile 时 sys.argv[0] 是引导 exe（真实存在），sys.executable 是解包
    临时目录里的假 python.exe，必须先信 argv[0]。源码态返回当前解释器。
    """
    for path in (sys.argv[0] if sys.argv else "", sys.executable):
        if path and os.path.isfile(path):
            # realpath 而不是 abspath：Linux 上经 /usr/bin 软链启动时，abspath 会把
            # data/ 定到链接所在的只读目录，书架与代理记忆当场「消失」
            return os.path.realpath(path)
    return sys.executable
