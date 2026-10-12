"""更新提醒：每本小说一张卡片，卡片固定列最近 5 章并区分已读/未读。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .align import display_title
from .models import ChapterEntry, NovelAlert
from .packaging import is_packaged, self_executable
from .popup import make_payload

_CARD_CHAPTERS = 5
_CARD_LINES = 4
_STARTUP_GRACE_SECONDS = 0.5
_CARD_SECONDS = 12.0
# 槽位占用时长比卡片展示略长：两批更新间隔再短，新的一批也会排到没释放的槽位之后
_SLOT_HOLD_SECONDS = _CARD_SECONDS + 0.5


class Notifier:
    def __init__(self, alerts_path: Path | None = None, enabled: bool = True):
        self.alerts_path = alerts_path
        self.enabled = enabled
        # 每个卡片槽位的释放时刻（monotonic 秒）；过期的槽位在分配时顺手清掉
        self._slots: list[float] = []

    # ---------- 提醒入口 ----------

    def notify_alert(self, alert: NovelAlert) -> bool:
        """一本小说一张卡片：行取这本书「最近的 5 章」，每行标出最先报这一章的来源。"""
        novel = alert.novel
        rows = entry_rows(novel.chapter_index, novel.read_keys, novel.site_of)
        if not rows:
            return False
        # 日志记的就是卡片上显示的那几行：alerts.log 要能对上现场，别记另一套章节
        record = [row["t"] for row in rows]
        return self._present(make_payload(alert.title(), chapters=rows, novel=novel.id),
                             record, self._take_slot(), rows[0]["u"])

    def notify_many(self, alerts: list[NovelAlert]) -> bool:
        """每本一张卡片，各自占一个未释放的槽位（popup 再按屏幕高度自动换列），不合并成一条。"""
        for alert in alerts:
            self.notify_alert(alert)
        return bool(alerts)

    def _take_slot(self) -> int:
        """给下一张卡分一个当前没被占用的序号：12 秒内连来两批更新也不会上下重叠。"""
        now = time.monotonic()
        active = sorted(until for until in self._slots if until > now)
        index = len(active)  # 存活中的卡片占着 0..n-1，新卡顺延到 n
        self._slots = active + [now + _SLOT_HOLD_SECONDS]
        return index

    def show(self, title: str, lines: list[str], url: str = "") -> bool:
        """普通提示（启动摘要、菜单操作反馈）：只有文字，没有可点章节。

        同样走槽位分配：启动卡/菜单回执也可能和上一批没消失的更新卡撞位置。
        """
        launch = _safe_url(url)
        payload = make_payload(title, lines=lines[:_CARD_LINES])
        self._record(title, lines, launch)
        return self._spawn(payload, self._take_slot())

    # ---------- 内部 ----------

    def _present(self, payload: dict, record: list[str], index: int, url: str) -> bool:
        launch = _safe_url(url)
        self._record(payload["title"], record, launch)
        return self._spawn(payload, index, _CARD_SECONDS)

    def _spawn(self, payload: dict, index: int = 0, seconds: float = _CARD_SECONDS) -> bool:
        """弹不出来时把原因补记一行：现场只有 alerts.log 一份东西可看。"""
        if not self.enabled:
            return False
        if spawn_card(payload, index, seconds):
            return True
        self._record(f"{payload['title']} · 卡片没弹出", [LAST_SPAWN_ERROR or "原因未知"], "")
        return False

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


LAST_SPAWN_ERROR = ""


def spawn_card(payload: dict, index: int = 0, seconds: float = _CARD_SECONDS) -> bool:
    """拉起卡片进程；它活过了启动窗口就算弹出来了。

    卡片是这工具唯一的反馈通道，起不来一定要留下原因，否则现场只能靠猜。
    """
    global LAST_SPAWN_ERROR
    LAST_SPAWN_ERROR = ""
    command = popup_command(payload, index, seconds)
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        LAST_SPAWN_ERROR = f"起进程失败 {type(exc).__name__}: {exc}（命令 {command[0]!r}）"
        return False
    try:
        code = proc.wait(timeout=_STARTUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return True
    if code != 0:
        LAST_SPAWN_ERROR = f"卡片进程秒退 exit={code}，命令 {command[1:3]}"
    return code == 0


def popup_command(payload: dict, index: int, seconds: float) -> list[str]:
    """构造拉起 popup 子进程的命令行。

    源码态：python -m shunshu popup ...
    打包态（Nuitka/PyInstaller）：shunshu.exe popup ...  ← 同一个 exe 通过子命令分流
    形态判定统一走 shunshu.packaging，别在本模块再猜 sys.frozen。
    """
    head = [self_executable(), "popup"] if is_packaged() else [sys.executable, "-m", "shunshu", "popup"]
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
