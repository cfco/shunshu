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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "popup":
        from .popup import show

        show(json.loads(args.payload), args.index, args.seconds)
        return 0

    from .app import AppState
    from .tray import TrayApp

    TrayApp(AppState()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
