"""共享测试装置：内存 Fetcher、默认规则、临时 data 目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures import BASE_URL  # noqa: F401  (供各测试文件复用)
from shunshu.config import Config
from shunshu.http import FetchError
from shunshu.store import Paths


class FakeFetcher:
    """按 URL 返回预置 HTML；pages 里没有的 URL 一律报网络错误。"""

    def __init__(self, pages: dict[str, str | bytes] | None = None):
        self.pages = dict(pages or {})
        self.calls: list[str] = []
        self.proxies: list[str] = []

    def add(self, url: str, html: str | bytes) -> None:
        self.pages[url] = html

    def fetch_html(self, url: str, proxy: str = "") -> str:
        self.calls.append(url)
        self.proxies.append(proxy)
        if url not in self.pages:
            raise FetchError(f"模拟站点没有这个页面: {url}")
        body = self.pages[url]
        return body.decode("utf-8") if isinstance(body, bytes) else body


class FakeRoot:
    """卡片逻辑只需要 root.destroy()，不必真起一个 Tk 窗口。"""

    def __init__(self):
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


class FakeProc:
    """卡片进程的替身：主进程对它的了解只有「以什么码退出的」和「关掉它」。

    code=0 是用户自己关的（划过或点掉，算看过了）；非 0 是被 terminate、崩了或被系统
    杀掉（人根本没看到，不算）。terminate() 故意按真进程那样留 1。
    """

    def __init__(self, exited: bool = False, code: int = 0):
        self.exited = exited
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code if self.exited else None

    def terminate(self) -> None:
        self.terminated = True
        self.exited = True
        self.code = 1


class FakePointerRoot:
    """判鼠标位置的卡片逻辑用的假窗口：after 队列攥在测试手里，一步一喂鼠标坐标。"""

    def __init__(self, box=(100, 100, 400, 190), pointer=(0, 0)):
        self.box = box  # (left, top, width, height)
        self.pointer = pointer
        self.destroyed = False
        self._queue = []

    def winfo_rootx(self) -> int:
        return self.box[0]

    def winfo_rooty(self) -> int:
        return self.box[1]

    def winfo_reqwidth(self) -> int:
        return self.box[2]

    def winfo_reqheight(self) -> int:
        return self.box[3]

    def winfo_width(self) -> int:
        return self.box[2]

    def winfo_height(self) -> int:
        return self.box[3]

    def winfo_pointerxy(self) -> tuple[int, int]:
        return self.pointer

    def after(self, _ms: int, fn) -> None:
        self._queue.append(fn)

    def destroy(self) -> None:
        self.destroyed = True

    def move(self, x: int, y: int) -> None:
        self.pointer = (x, y)

    def tick(self) -> None:
        """跑掉一次采样（卡片收起后不再排新的，队列就空了）。"""
        if self._queue:
            self._queue.pop(0)()


@pytest.fixture(autouse=True)
def no_real_cards(monkeypatch):
    """卡片是子进程 + 真窗口，测试里不能真弹，否则桌面被糊满。

    返回一个 FakeProc：卡片不再自己掐秒消失，主进程要靠这个句柄记账（去重、封顶、
    回收被划走的），返回 None 就等于「弹完立刻退出」，那条路另有用例专门覆盖。
    """
    monkeypatch.setattr("shunshu.notify.spawn_card", lambda payload, index=0: ("", FakeProc()))


def capture_cards(monkeypatch) -> list:
    """把 spawn_card 换成假进程并逐张记账：测试要看得见弹了几张、哪张还挂着。"""
    popped: list = []

    def fake_spawn(payload, index=0):
        proc = FakeProc()
        popped.append((payload, index, proc))
        return "", proc

    monkeypatch.setattr("shunshu.notify.spawn_card", fake_spawn)
    return popped


@pytest.fixture
def rules() -> Config:
    return Config().compiled()


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    instance = Paths(tmp_path / "data")
    instance.ensure()
    return instance


@pytest.fixture
def app_state(tmp_path, monkeypatch):
    """全新临时 data 目录上的 AppState；各测试文件共用，添加/探测流程都靠它。"""
    from shunshu.app import AppState

    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    state = AppState(Paths(tmp_path / "data"))
    state.reload()
    return state
