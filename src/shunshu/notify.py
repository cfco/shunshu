"""更新提醒：每本小说一张卡片，卡片固定列最近 5 章并区分已读/未读。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .align import display_title
from .models import ChapterEntry, NovelAlert
from .packaging import is_packaged, self_executable
from .popup import make_payload

_CARD_CHAPTERS = 5
_CARD_LINES = 4
_STARTUP_GRACE_SECONDS = 0.5
# 卡片不再自己掐秒消失，右边那一叠总有摆完的时候：3 行 × 3 列（popup._place 按屏幕高度
# 换列，1080p 上一列正好 3 张）之后再弹就收掉最旧那张，别让没被划过的卡压到屏幕外去
_MAX_LIVE_CARDS = 9


class Notifier:
    def __init__(self, alerts_path: Path | None = None, enabled: bool = True):
        self.alerts_path = alerts_path
        self.enabled = enabled
        # 还挂在屏幕上的卡片：键（哪本书 / 哪句提示）→ (卡片进程, 槽位号)。卡片活到被看过
        # 为止，父进程只能靠自己攥着进程句柄来知道谁还在、谁占了哪个槽位；插入顺序＝从旧到新
        self._live: dict[str, tuple[object | None, int]] = {}
        self._generations: dict[str, int] = {}
        self._card_keys: dict[str, frozenset[str]] = {}
        self._generation = 0
        # 已经被人看过（卡片自己退出的）的键及其章节快照，等主程序来收走并清它的补弹账
        self._acked: dict[str, set[str]] = {}
        # 定时轮与添加书源首轮可能在两条线程上同时弹卡：分配与记账必须互斥，否则
        # 两边各自数出同一个槽位号，两张卡叠在同一位置
        self._lock = threading.Lock()

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
                             record, novel.id, rows[0]["u"], {entry.k for entry in alert.entries})

    def notify_many(self, alerts: list[NovelAlert]) -> bool:
        """每本一张卡片，各自占一个空着的槽位（popup 再按屏幕高度自动换列），不合并成一条。"""
        results = [self.notify_alert(alert) for alert in alerts]
        return bool(alerts) and all(results)

    def has_live(self, key: str) -> bool:
        with self._lock:
            self._collect_acks()
            return key in self._live

    def show(self, title: str, lines: list[str], url: str = "") -> bool:
        """普通提示（启动摘要、菜单操作反馈）：只有文字，没有可点章节。

        同样占槽位、同样按标题去重：托盘回执和没消失的更新卡撞位置会互相盖住。
        """
        launch = _safe_url(url)
        shown = lines[:_CARD_LINES]  # 卡片只显示这几行，日志也记这几行，现场才对得上
        payload = make_payload(title, lines=shown)
        self._record(title, shown, launch)
        return self._spawn(payload, title, set())

    # ---------- 内部 ----------

    def _present(self, payload: dict, record: list[str], key: str, url: str,
                 chapter_keys: set[str] | None = None) -> bool:
        launch = _safe_url(url)
        self._record(payload["title"], record, launch)
        return self._spawn(payload, key, chapter_keys or set())

    def _reserve(self, key: str, chapter_keys: set[str]) -> tuple[int, int, frozenset[str]] | None:
        """给这张卡腾出槽位：同一本书的旧卡先收掉，整屏摆满就收掉最旧那张。

        先把槽位记成 (None, index) 占住，弹完再由 _settle 补上进程或把槽位还回去——
        占住的过程不能被别的线程插进来，否则两边会数出同一个号。
        """
        with self._lock:
            if not self.enabled:
                return None
            self._collect_acks()
            self._retire(key)
            while len(self._live) >= _MAX_LIVE_CARDS:
                self._retire(next(iter(self._live)))
            used = {index for _proc, index in self._live.values()}
            index = next(i for i in range(len(used) + 1) if i not in used)
            self._generation += 1
            generation = self._generation
            self._live[key] = (None, index)
            self._generations[key] = generation
            snapshot = frozenset(chapter_keys)
            self._card_keys[key] = snapshot
            return index, generation, snapshot

    def _collect_acks(self) -> None:
        """把已经消失的卡片从账上摘掉，自己退出去的那张记成「看过了」。

        卡片只有三种下场：被点掉/被鼠标划过（自己退出，退出码 0）、被我们 terminate
        （同书换新章、堆满淘汰——那种人根本没看到，不算看过，所以走 _retire 那条路
        直接摘账、不进这里）、以及崩了或被系统杀掉（退出码非 0，同样不算看过）。
        摘账也是必须的：不然槽位号越数越多，新卡一路往屏幕外排。
        """
        for key, (proc, _index) in list(self._live.items()):
            if proc is None:
                continue          # 别的线程正在弹的那张，进程还没起来
            code = proc.poll()
            if code is None:
                continue          # 还挂在屏幕上
            del self._live[key]
            self._generations.pop(key, None)
            snapshot = self._card_keys.pop(key, frozenset())
            if code == 0:
                self._acked.setdefault(key, set()).update(snapshot)
            elif not self.enabled:
                continue          # 正在退出，被我们 terminate 的那些不算异常
            else:
                # 活过 0.5 秒宽限期之后才崩的卡片（慢冷启、缺 Tk 依赖、坏 payload）：
                # 子进程 stderr 是 DEVNULL，不留这一行就完全无从查起，而它本该是唯一的反馈通道
                self._record(f"卡片进程 {key}", [f"退出码 {code}（这张卡没让人看到）"], "")

    def take_acked(self) -> list[str]:
        """主程序每轮来收一次：这些键的卡片已经被用户看过了，可以清补弹的账。"""
        return sorted(self.take_acked_chapters())

    def take_acked_chapters(self) -> dict[str, set[str]]:
        with self._lock:
            self._collect_acks()
            acked = {key: set(keys) for key, keys in self._acked.items()}
            self._acked.clear()
            return acked

    def close_all(self) -> dict[str, set[str]]:
        """退出时收掉窗口，并交回已确认看过的 ack；调用方忽略返回值仍可正常关机。"""
        # 先关闸：正在跑的那一轮可能在我收完窗口之后才 notify_many，那时再 spawn 就是一张
        # 永远没人划过、也没人收的无主卡片（主进程已经退出了，句柄跟着没了）
        with self._lock:
            self.enabled = False
            self._collect_acks()
            acked = {key: set(keys) for key, keys in self._acked.items()}
            live, self._live = self._live, {}
            self._generations.clear()
            self._card_keys.clear()
            self._acked.clear()
        for proc, _index in live.values():
            if proc is not None:
                self._close(proc)
        return acked

    def _settle(self, key: str, proc, index: int, generation: int,
                snapshot: frozenset[str]) -> None:
        stale = False
        seen = False
        with self._lock:
            if not self.enabled or self._generations.get(key) != generation:
                stale = True
                seen = bool(self.enabled and proc is not None and proc.poll() == 0)
                if seen:
                    self._acked.setdefault(key, set()).update(snapshot)
            elif proc is None:
                self._live.pop(key, None)
                self._generations.pop(key, None)
                self._card_keys.pop(key, None)
            else:
                self._live[key] = (proc, index)
        if stale and proc is not None and not seen:
            self._close(proc)

    def _retire(self, key: str) -> None:
        """把一张卡从账上摘掉，顺手关掉它（账上没有就什么都不做）。"""
        proc, _index = self._live.pop(key, (None, -1))
        self._generations.pop(key, None)
        self._card_keys.pop(key, None)
        if proc is None:
            return  # 别的线程正在弹的那张，进程还没起来
        self._close(proc)

    @staticmethod
    def _close(proc) -> None:
        try:
            proc.terminate()
        except OSError:
            pass  # 刚好已经自己退出的进程，TerminateProcess 会报错，结果一样：卡片不在了

    def _spawn(self, payload: dict, key: str, chapter_keys: set[str]) -> bool:
        """弹不出来时把原因补记一行：现场只有 alerts.log 一份东西可看。"""
        reserved = self._reserve(key, chapter_keys)
        if reserved is None:
            return False
        index, generation, snapshot = reserved
        error, proc = spawn_card(payload, index)
        self._settle(key, proc, index, generation, snapshot)
        if error:
            self._record(f"{payload['title']} · 卡片没弹出", [error], "")
            return False
        return True

    def _record(self, title: str, lines: list[str], url: str) -> None:
        if self.alerts_path is None:
            return
        from .store import append_text, now_stamp

        text = f"{now_stamp()} {title} | {' / '.join(lines)} | {url}"
        try:
            append_text(self.alerts_path, text)
        except (OSError, UnicodeError):
            try:
                append_text(Path(tempfile.gettempdir()) / f"shunshu-{self.alerts_path.name}", text)
            except (OSError, UnicodeError):
                pass  # 日志故障不能拦住弹卡


def entry_rows(entries: list[ChapterEntry], read_keys: list[str] | None = None,
               label_of=None) -> list[dict]:
    """章节索引 → 卡片行：标题转简体，带跨源身份 `k`（换个来源点开同一章也是灰的）和来源名 `s`。"""
    read = set(read_keys or [])
    rows: list[dict] = []
    for entry in entries[:_CARD_CHAPTERS]:
        rows.append({"t": display_title(entry.t), "u": entry.u, "k": entry.k,
                     "s": label_of(entry.s) if label_of else "", "read": entry.k in read})
    return rows


def spawn_card(payload: dict, index: int = 0) -> tuple[str, object | None]:
    """拉起卡片进程，返回（弹不出来的原因，进程句柄）；原因空串＝已经交给系统了。

    卡片是这工具唯一的反馈通道，起不来一定要留下原因，否则现场只能靠猜。原因走
    返回值、不走模块级变量：定时轮和「添加小说」可能同时各起一张卡，共享变量会让
    两边的失败原因串行，alerts.log 里就记到别人头上。
    句柄也要交回调用方：卡片不掐秒自动消失，屏幕上还挂着哪几张只能由父进程记账，
    去重和封顶都靠它（已退出的进程给 None，没什么可记的）。
    """
    command = popup_command(payload, index)
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return f"起进程失败 {type(exc).__name__}: {exc}（命令 {command[0]!r}）", None
    try:
        code = proc.wait(timeout=_STARTUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return "", proc   # 活过了启动窗口就算弹出来了（卡片会挂到被点或被划过）
    if code is None or code == 0:
        return "", proc   # 正常退出也要交回句柄，父进程才能把它记成「已经看过」
    return f"卡片进程秒退 exit={code}，命令 {command[1:3]}", None


def popup_command(payload: dict, index: int) -> list[str]:
    """构造拉起 popup 子进程的命令行。

    源码态：python -m shunshu popup ...
    打包态（Nuitka/PyInstaller）：shunshu.exe popup ...  ← 同一个 exe 通过子命令分流
    形态判定统一走 shunshu.packaging，别在本模块再猜 sys.frozen。
    """
    head = [self_executable(), "popup"] if is_packaged() else [sys.executable, "-m", "shunshu", "popup"]
    return [*head, "--payload", json.dumps(payload, ensure_ascii=False), "--index", str(index)]


def _safe_url(url: str) -> str:
    from .http import assert_http_url

    if not url:
        return ""
    try:
        return assert_http_url(url)
    except ValueError:
        return ""
