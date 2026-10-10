"""更新提醒：每本小说一张卡片，卡片固定列最近 5 章并区分已读/未读。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .align import display_title
from .models import ChapterEntry, NovelAlert
from .popup import make_payload

_CARD_CHAPTERS = 5
_CARD_LINES = 4
_STARTUP_GRACE_SECONDS = 0.5
_CARD_SECONDS = 12.0


class Notifier:
    def __init__(self, alerts_path: Path | None = None, enabled: bool = True):
        self.alerts_path = alerts_path
        self.enabled = enabled

    # ---------- 提醒入口 ----------

    def notify_alert(self, alert: NovelAlert, index: int = 0) -> bool:
        """一本小说一张卡片：行取这本书「最近的 5 章」，每行标出最先报这一章的来源。"""
        novel = alert.novel
        rows = entry_rows(novel.chapter_index, novel.read_keys, novel.site_of)
        if not rows:
            return False
        record = [row["t"] for row in entry_rows(alert.entries)][-_CARD_CHAPTERS:]
        return self._present(make_payload(alert.title(), chapters=rows, novel=novel.id),
                             record, index, rows[0]["u"])

    def notify_many(self, alerts: list[NovelAlert]) -> bool:
        """每本一张卡片，纵向错开，不合并成一条。"""
        for offset, alert in enumerate(alerts):
            self.notify_alert(alert, offset)
        return bool(alerts)

    def show(self, title: str, lines: list[str], url: str = "") -> bool:
        """普通提示（启动摘要、菜单操作反馈）：只有文字，没有可点章节。"""
        launch = _safe_url(url)
        payload = make_payload(title, lines=lines[:_CARD_LINES])
        self._record(title, lines, launch)
        return spawn_card(payload) if self.enabled else False

    # ---------- 内部 ----------

    def _present(self, payload: dict, record: list[str], index: int, url: str) -> bool:
        launch = _safe_url(url)
        self._record(payload["title"], record, launch)
        return spawn_card(payload, index, _CARD_SECONDS) if self.enabled else False

    def _record(self, title: str, lines: list[str], url: str) -> None:
        if self.alerts_path is None:
            return
        from .store import append_text, now_stamp

        append_text(self.alerts_path, f"{now_stamp()} {title} | {' / '.join(lines)} | {url}")


def entry_rows(entries: list[ChapterEntry], read_keys: list[str] | None = None,
               label_of=None) -> list[dict]:
    """章节索引 → 卡片行：标题转简体，带跨源身份 `k`（换个来源点开同一章也是灰的）和来源名 `s`。"""
    read = set(read_keys or [])
    rows: list[dict] = []
    for entry in entries[:_CARD_CHAPTERS]:
        rows.append({"t": display_title(entry.t), "u": entry.u, "k": entry.k,
                     "s": label_of(entry.s) if label_of else "", "read": entry.k in read})
    return rows


def spawn_card(payload: dict, index: int = 0, seconds: float = _CARD_SECONDS) -> bool:
    """拉起卡片进程；它活过了启动窗口就算弹出来了。"""
    try:
        proc = subprocess.Popen(
            popup_command(payload, index, seconds),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    try:
        code = proc.wait(timeout=_STARTUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return True
    return code == 0


def _is_compiled() -> bool:
    """打包态判定：PyInstaller 设 `sys.frozen`，Nuitka 给每个模块注入 `__compiled__`。

    两者都要认：只查 frozen 的话，Nuitka 编出来的 exe 会以为自己是源码态，
    去拼 `[exe, "-m", "shunshu", "popup"]`，卡片就弹不出来。
    """
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def popup_command(payload: dict, index: int, seconds: float) -> list[str]:
    """构造拉起 popup 子进程的命令行。

    源码态：python -m shunshu popup ...
    打包态（Nuitka/PyInstaller）：shunshu.exe popup ...  ← 同一个 exe 通过子命令分流
    """
    head = [sys.executable, "popup"] if _is_compiled() else [sys.executable, "-m", "shunshu", "popup"]
    return [*head, "--payload", json.dumps(payload, ensure_ascii=False),
            "--index", str(index), "--seconds", f"{seconds:g}"]


def _safe_url(url: str) -> str:
    from .http import assert_http_url

    if not url:
        return ""
    try:
        return assert_http_url(url)
    except ValueError:
        return ""
