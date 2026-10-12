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


@pytest.fixture(autouse=True)
def no_real_cards(monkeypatch):
    """卡片是子进程 + 真窗口，测试里不能真弹，否则桌面被糊满。"""
    monkeypatch.setattr("shunshu.notify.spawn_card", lambda payload, index=0, seconds=12.0: True)


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
