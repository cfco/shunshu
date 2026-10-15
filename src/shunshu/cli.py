"""入口：`shunshu` 起托盘常驻；`popup` 是主程序弹卡片时调用的内部子命令。

日常操作都在托盘右键菜单里，书架与代理直接改 `data/novels.json`，
所以这里不对外提供 add/list/check 之类的子命令。
"""

from __future__ import annotations

import argparse
import json
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shunshu", description="小说目录页更新监控（托盘常驻 + 弹窗提醒）",
        epilog="日常操作都在托盘右键菜单里；书架与代理直接改 data/novels.json。")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动托盘常驻（默认）")
    popup = sub.add_parser("popup", help="内部：主程序弹卡片时调用")
    popup.add_argument("--payload", required=True, help="卡片内容的 JSON（见 shunshu.popup.make_payload）")
    popup.add_argument("--index", type=int, default=0)
    popup.add_argument("--seconds", type=float, default=12.0)
    return parser


def _report_crash(exc: BaseException) -> None:
    """启动崩了要把话留下来：编译版用 `--windows-console-mode=disable`，没有控制台，
    traceback 不落到文件上就是彻底无声（数据目录本身写不动时，连 data/ 都指不上）。

    顺序：`data/shunshu.log` → 系统临时目录；卡片是尽力而为，弹不出也不能盖掉已写的日志。
    """
    import tempfile
    import traceback
    from pathlib import Path

    from . import __version__
    from .store import Paths, append_text, now_stamp

    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    paths = Paths()
    for target in (paths.runtime_log, Path(tempfile.gettempdir()) / "shunshu-crash.log"):
        try:
            append_text(target, f"{now_stamp()} 启动失败（v{__version__}）:\n{detail}")
            break
        except OSError:
            continue
    try:
        from .notify import Notifier

        Notifier(paths.alerts_log).show(
            "顺手启动失败",
            [f"v{__version__}", f"{type(exc).__name__}: {exc}", f"详情见 {paths.runtime_log}"],
            "")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "popup":
        from .popup import show

        show(json.loads(args.payload), args.index, args.seconds)
        return 0

    try:
        from .app import AppState
        from .tray import TrayApp

        TrayApp(AppState()).run()
    except Exception as exc:
        # 起不来时进程静默退出，托盘图标、卡片、日志一个都不会有——先留痕再退非零码
        _report_crash(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
