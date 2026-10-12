"""右下角提醒卡片：由主程序以子进程方式拉起，点章节即跳浏览器。

不用 Windows 原生 toast：非打包程序的 AUMID 注册不上，WinRT 会把通知静默丢掉
（Show() 不报错，但通知中心的库里根本没写入）。这里用 Tk 画一张无边框卡片。
已读状态由卡片自己追加写 data/read.jsonl，主程序下一轮吸收进书架。
"""

from __future__ import annotations

import argparse
import json
import sys
import tkinter as tk
from tkinter import font as tkfont

_WIDTH = 400
_MARGIN_X = 24
_MARGIN_Y = 44
_MARGIN_BOTTOM = 24
# 卡片高度只由「标题 1 行 + 最多 5 章 1 行」决定（长标题按像素截断，不换行），
# 所以堆叠间距可以是固定值：小于最矮的满卡就会压住上一张。
_CARD_GAP = 258
# 一列卡片之间的横向间距（排满右列后向左换列）
_COLUMN_GAP = 12
_INNER_WIDTH = _WIDTH - 60
_BG = "#22262e"
_FG = "#f2f3f5"
_ACCENT = "#7fb3ff"
_UNREAD = "#6fd08c"
_READ = "#7d8590"
_FONT = "Microsoft YaHei UI"


def make_payload(title: str, lines: list[str] | None = None, chapters: list[dict] | None = None,
                 novel: str = "") -> dict:
    return {"title": title, "lines": lines or [], "chapters": chapters or [], "novel": novel}


def _rows_per_column(root: tk.Tk) -> int:
    """按屏幕高度算右边缘这一列能放几张卡；屏幕再小也至少放 1 张。"""
    available = root.winfo_screenheight() - _MARGIN_Y - _MARGIN_BOTTOM
    return max(1, available // _CARD_GAP)


def _place(root: tk.Tk, index: int, width: int = _WIDTH) -> None:
    """先排布再量尺寸，否则 overrideredirect 窗口会塌成一条。

    卡片从右下角向上堆叠：一列排满（按屏幕高度算行数）就向左换一列，
    index 由 Notifier 按槽位分配，保证短时间内的多批卡片不会完全重叠。
    """
    root.update_idletasks()
    height = root.winfo_reqheight()
    rows = _rows_per_column(root)
    column, row = divmod(max(0, index), rows)
    x = max(0, root.winfo_screenwidth() - width - _MARGIN_X
            - column * (width + _COLUMN_GAP))
    y = _MARGIN_Y + row * _CARD_GAP
    root.geometry(f"{width}x{height}+{x}+{y}")


def _mark_read(novel_id: str, chapter: dict) -> None:
    from .store import Paths, append_jsonl, now_stamp

    append_jsonl(Paths().read_log, {
        "ts": now_stamp(), "novel": novel_id, "key": chapter.get("k") or "",
        "url": chapter.get("u") or "",
    })


def open_chapter(root: tk.Tk, payload: dict, chapter: dict) -> None:
    """点章节：交给默认浏览器 + 记一笔已读，然后收起卡片。"""
    url = chapter.get("u") or ""
    key = chapter.get("k") or ""
    if url:
        from .browser import open_in_default_browser

        if open_in_default_browser(url) and payload.get("novel") and key:
            _mark_read(payload["novel"], chapter)
    root.destroy()


def _ellipsis_fit(text: str, measure, width: int) -> str:
    """按像素宽度截断成一行，保证卡片高度只跟「行数」有关。"""
    if measure(text) <= width:
        return text
    tail = "…"
    while text and measure(text + tail) > width:
        text = text[:-1]
    return text + tail


def build_card(root: tk.Tk, payload: dict) -> None:
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-toolwindow", True)
    root.configure(bg=_BG)

    title_font = tkfont.Font(font=(_FONT, 11, "bold"))
    line_font = tkfont.Font(font=(_FONT, 10))
    fit = lambda text, f, w=_INNER_WIDTH: _ellipsis_fit(text, f.measure, w)

    body = tk.Frame(root, bg=_BG, padx=14, pady=12)
    body.pack(fill="both", expand=True)
    tk.Label(body, text="顺手", bg=_BG, fg=_ACCENT, font=(_FONT, 9)).pack(anchor="w")
    tk.Label(body, text=fit(payload.get("title", ""), title_font, _INNER_WIDTH),
             bg=_BG, fg=_FG, font=title_font, justify="left", anchor="w").pack(anchor="w", pady=(2, 6))

    def dismiss(_event=None) -> None:
        root.destroy()

    for line in payload.get("lines") or []:
        tk.Label(body, text=fit(line, line_font), bg=_BG, fg="#d3d7de", font=line_font,
                 justify="left", anchor="w").pack(anchor="w")

    for chapter in payload.get("chapters") or []:
        row = tk.Frame(body, bg=_BG)
        row.pack(anchor="w", fill="x")
        site = str(chapter.get("s") or "").strip()
        tag = None
        room = _INNER_WIDTH
        if site:
            # 多来源时得看清这一章是哪一站报的：来源名右对齐，标题按剩下的宽度截断
            tag = tk.Label(row, text=_ellipsis_fit(site, line_font.measure, 120), bg=_BG,
                           fg="#5f6773", font=line_font)
            tag.pack(side="right", padx=(10, 0))
            row.update_idletasks()
            room = max(80, _INNER_WIDTH - tag.winfo_reqwidth() - 10)
        label = tk.Label(row, text=fit(chapter.get("t", ""), line_font, room), bg=_BG,
                         fg=_READ if chapter.get("read") else _UNREAD, font=line_font,
                         justify="left", anchor="w", cursor="hand2")
        label.pack(side="left")
        for widget in (label, tag):
            if widget is not None:
                widget.bind("<Button-1>", lambda _event, c=chapter: open_chapter(root, payload, c))

    footer = tk.Frame(body, bg=_BG)
    footer.pack(fill="x", pady=(10, 0))
    tk.Button(footer, text="关闭", command=dismiss, bg="#2c313a", fg="#c9ced7",
              activebackground="#3a4150", activeforeground=_FG, relief="flat", bd=0,
              font=(_FONT, 9), padx=12, pady=2).pack(side="right")


def show(payload: dict, index: int = 0, seconds: float = 10.0) -> None:
    root = tk.Tk(className="shunshu")
    root.title("顺手")
    build_card(root, payload)
    _place(root, index)
    root.after(max(2000, int(seconds * 1000)), root.destroy)
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shunshu.popup", description="顺手更新提醒卡片")
    parser.add_argument("--payload", required=True, help="make_payload 生成的 JSON 字符串")
    parser.add_argument("--index", type=int, default=0, help="多本同时更新时的纵向堆叠序号")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    show(json.loads(args.payload), args.index, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
