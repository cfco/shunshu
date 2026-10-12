"""添加书源时代理自动探测的接入测试：AppState 记忆/落盘/复用 + 托盘添加流程。

网络层全部用脚本化假 fetcher（只实现 fetch_html(url, proxy="")），
candidate_proxies/tcp_reachable 也 monkeypatch 掉，不真联网。
"""

from __future__ import annotations

import pytest

from shunshu.app import AppState
from shunshu.http import ConnectionFailureError, FetchError, ProxyError
from shunshu.proxy_detect import ProbeResult
from shunshu import proxy_detect


class _DirectOnly:
    """直连才通；一旦给了代理就视为错误（验证直连成功时绝不去试代理）。"""

    def __init__(self):
        self.tried: list[str] = []

    def fetch_html(self, url: str, proxy: str = "") -> str:
        self.tried.append(proxy)
        if proxy:
            raise FetchError("这个站直连就通，不该走代理")
        return "<html>ok</html>"


class _NeedProxy:
    """直连连不上（连接级失败），只有指定代理能拿到页面；记录所有尝试顺序。"""

    def __init__(self, working: str = "http://127.0.0.1:7890"):
        self.working = working
        self.tried: list[str] = []

    def fetch_html(self, url: str, proxy: str = "") -> str:
        self.tried.append(proxy)
        if not proxy:
            # 直连必须是「连接没建立」才会触发切代理
            raise ConnectionFailureError("直连 443 不通")
        if proxy != self.working:
            raise ProxyError(f"{proxy} 端口开着但不是代理")
        return "<html>ok</html>"


class _Dead:
    """直连连不上，代理候选端口又全没监听：探测结论必须是 None。"""

    def fetch_html(self, url: str, proxy: str = "") -> str:
        if proxy:
            raise AssertionError("TCP 预探活该把死端口刷掉，不该走到 HTTP 探测")
        raise ConnectionFailureError("完全不可达")


@pytest.fixture
def one_proxy_candidate(monkeypatch):
    """探测环境固定为：只有 7890 一个候选且端口开着。"""
    monkeypatch.setattr(proxy_detect, "candidate_proxies",
                        lambda: ["http://127.0.0.1:7890"])
    monkeypatch.setattr(proxy_detect, "tcp_reachable", lambda url, timeout=1.0: True)


@pytest.fixture
def no_reachable_proxy(monkeypatch):
    """探测环境固定为：候选存在但端口全都没监听。"""
    monkeypatch.setattr(proxy_detect, "candidate_proxies",
                        lambda: ["http://127.0.0.1:7890"])
    monkeypatch.setattr(proxy_detect, "tcp_reachable", lambda url, timeout=1.0: False)


# ---------- AppState.ensure_source_proxy ----------

def test_direct_site_is_remembered_as_direct_and_persisted(app_state: AppState):
    novel = app_state.add_novel("https://m.direct-book.example/123/")
    app_state.save_novels()

    fetcher = _DirectOnly()
    result = app_state.ensure_source_proxy(novel.sources[0].id, fetcher)
    assert result == ProbeResult(proxy="", html="<html>ok</html>")
    assert result.proxy == ""
    assert fetcher.tried == [""], "直连成功后不许再试代理"
    assert novel.sources[0].proxy == ""
    assert app_state.cfg.site_proxies["m.direct-book.example"] == ""

    # 结论必须落盘：重启式 reload 后记忆还在
    app_state.reload()
    assert app_state.cfg.site_proxies["m.direct-book.example"] == ""


def test_proxy_site_is_remembered_and_written_onto_source(app_state: AppState, one_proxy_candidate):
    novel = app_state.add_novel("https://m.proxy-book.example/123/")
    app_state.save_novels()

    result = app_state.ensure_source_proxy(novel.sources[0].id, _NeedProxy())
    assert result.proxy == "http://127.0.0.1:7890"
    assert result.html == "<html>ok</html>", "探测抓回的首页 HTML 要带回给首轮建档复用"
    assert novel.sources[0].proxy == "http://127.0.0.1:7890", "后续每一轮要走代理，必须写进来源"
    assert app_state.cfg.site_proxies["m.proxy-book.example"] == "http://127.0.0.1:7890"

    app_state.reload()
    novel2 = app_state.novels[0]
    assert app_state.cfg.site_proxies["m.proxy-book.example"] == "http://127.0.0.1:7890"
    assert novel2.sources[0].proxy == "http://127.0.0.1:7890", "novels.json 也要随结论落盘"


def test_same_site_second_source_reuses_memory_without_probing(app_state: AppState,
                                                               one_proxy_candidate):
    novel = app_state.add_novel("https://m.proxy-book.example/123/")
    app_state.ensure_source_proxy(novel.sources[0].id, _NeedProxy())

    # 同一站点再添一个来源：直接套站点记忆，一个网络请求都不该发（也没有 HTML 可复用）
    source2 = app_state.add_source(novel.id, "https://m.proxy-book.example/99999999/")
    app_state.save_novels()
    fetcher = _NeedProxy()
    result = app_state.ensure_source_proxy(source2.id, fetcher)
    assert result == ProbeResult(proxy="http://127.0.0.1:7890", probed=False)
    assert fetcher.tried == []
    assert source2.proxy == "http://127.0.0.1:7890"


def test_www_and_apex_share_memory_key(app_state: AppState):
    """www 归一：www.book.example 探测过直连，book.example 之外同主机的 www 形态直接复用。"""
    novel = app_state.add_novel("https://www.direct-book.example/1/")
    app_state.ensure_source_proxy(novel.sources[0].id, _DirectOnly())
    assert "www.direct-book.example" not in app_state.cfg.site_proxies
    assert app_state.cfg.site_proxies["direct-book.example"] == ""


def test_total_failure_remembers_nothing(app_state: AppState, no_reachable_proxy):
    novel = app_state.add_novel("https://dead.example/1/")
    app_state.save_novels()

    assert app_state.ensure_source_proxy(novel.sources[0].id, _Dead()) is None
    assert app_state.cfg.site_proxies == {}, "全不通不能记忆，否则下次添加同站将永远不重试"
    assert novel.sources[0].proxy == ""


def test_manual_proxy_is_respected_and_never_probed(app_state: AppState):
    class _Boom:
        def fetch_html(self, url: str, proxy: str = "") -> str:
            raise AssertionError("来源已手写 proxy，绝不该再探测")

    novel = app_state.add_novel("https://book.example/1/")
    novel.sources[0].proxy = "socks5://10.0.0.9:1080"
    result = app_state.ensure_source_proxy(novel.sources[0].id, _Boom())
    # probed=False：一个包都没试过。代理失效自动重探那条路靠它区分「探通了」和「没探」，
    # 否则死掉的手写代理每轮到阈值就被清一次失败计数，永远退避不起来
    assert result == ProbeResult(proxy="socks5://10.0.0.9:1080", probed=False)


def test_missing_source_id_returns_none_without_error(app_state: AppState):
    assert app_state.ensure_source_proxy("不存在的id", _Dead()) is None


# ---------- 托盘添加流程 ----------

def _silent_tray(app_state: AppState, monkeypatch):
    """构造 TrayApp 但把所有 UI 动作换成记录，测试只关心流程分支。"""
    from shunshu.tray import TrayApp

    tray = TrayApp(app_state)
    monkeypatch.setattr(tray, "_rebuild_menu", lambda: None)
    return tray


def test_tray_add_novel_runs_first_check_after_probe_ok(app_state: AppState, monkeypatch):
    # 探测直连成功，并带回了首页 HTML：首轮 check_one 必须通过 prefetch 复用它
    monkeypatch.setattr(app_state, "ensure_source_proxy",
                        lambda sid, fetcher=None: ProbeResult(proxy="", html="<html>ok</html>"))
    calls: list[dict] = []

    def fake_check_one(ident, notify=True, prefetch=None):
        calls.append({"ident": ident, "prefetch": prefetch})
        return []

    monkeypatch.setattr(app_state, "check_one", fake_check_one)

    tray = _silent_tray(app_state, monkeypatch)
    monkeypatch.setattr(tray, "_flash_outcomes", lambda outcomes: None)
    tray._check_new_novel("novel-1")

    assert calls and calls[0]["ident"] == "novel-1", "探测通过后必须接着跑首轮建档"
    assert calls[0]["prefetch"] == {"novel-1": "<html>ok</html>"}, "探测抓回的 HTML 要透传给首轮"


def test_tray_add_novel_skips_check_and_shows_guide_when_unreachable(app_state: AppState,
                                                                     monkeypatch):
    monkeypatch.setattr(app_state, "ensure_source_proxy",
                        lambda sid, fetcher=None: None)
    checked: list[str] = []

    monkeypatch.setattr(app_state, "check_one",
                        lambda ident, notify=True, prefetch=None: checked.append(ident) or [])

    flashes: list[tuple[str, str]] = []
    tray = _silent_tray(app_state, monkeypatch)
    monkeypatch.setattr(tray, "_flash", lambda title, msg: flashes.append((title, msg)))
    tray._check_new_novel("novel-1")

    assert checked == [], "直连代理都不通时不跑首轮，避免再弹一张重复的错误卡"
    assert flashes, "必须给出可操作的指引（手改 novels.json / site_proxies）"
    assert "novels.json" in flashes[0][1]


def test_tray_add_source_probes_before_first_check(app_state: AppState, monkeypatch):
    probed: list[str] = []
    monkeypatch.setattr(app_state, "ensure_source_proxy",
                        lambda sid, fetcher=None:
                            probed.append(sid) or ProbeResult(proxy="http://127.0.0.1:7890",
                                                              html="<html>ok</html>"))
    calls: list[dict] = []

    def fake_check_one(ident, notify=True, prefetch=None):
        calls.append({"ident": ident, "prefetch": prefetch})
        return []

    monkeypatch.setattr(app_state, "check_one", fake_check_one)

    tray = _silent_tray(app_state, monkeypatch)
    monkeypatch.setattr(tray, "_flash", lambda title, msg: None)
    tray._check_new_source("novel-1", "source-9")

    assert probed == ["source-9"]
    # 新来源探测时的 HTML 按「来源 id」为 key 透传（不是小说 id）
    assert calls == [{"ident": "novel-1", "prefetch": {"source-9": "<html>ok</html>"}}]


def test_dead_manual_proxy_keeps_backing_off(app_state: AppState):
    """手写的代理死了要照常进退避，不能被「假重探」说成已经探通了。

    force 重探碰到手写覆盖时根本不发请求；把这种「没探」当成功，失败计数每轮到阈值
    就被清零，死代理每 5 轮被重新敲一次，日志还会谎报改走了新代理。
    """
    from shunshu.models import Outcome, STATUS_PROXY_ERROR

    novel = app_state.add_novel("https://book.example/1/")
    source = novel.sources[0]
    source.proxy = "socks5://10.0.0.9:1080"
    threshold = app_state.cfg.failure_backoff_threshold
    source.consecutive_failures = threshold
    source.last_error = "10.0.0.9:1080 走不通"

    app_state._reprobe_dead_proxies(
        [Outcome(novel=novel, source=source, status=STATUS_PROXY_ERROR)])

    assert source.consecutive_failures == threshold, "计数不能被清零，否则永远退避不起来"
    assert source.last_error == "10.0.0.9:1080 走不通", "错误现场得留着"
    if app_state.paths.runtime_log.exists():
        assert "自动重探" not in app_state.paths.runtime_log.read_text(encoding="utf-8")


def test_skipped_proxy_error_source_is_not_reprobed(app_state: AppState, monkeypatch):
    """被退避跳过的那一轮不触发代理重探：那一轮根本没敲门，重探是白敲（最坏约一分钟）。

    跳过轮的 outcome 仍带着 proxy_error 状态与到阈值的失败计数，run_round 必须按 skip_ids
    把它从重探名单里剔掉，否则死代理每个跳过轮都被重探一次。
    """
    from shunshu.models import STATUS_PROXY_ERROR

    novel = app_state.add_novel("https://proxy-book.example/1/")
    source = novel.sources[0]
    source.proxy = "http://127.0.0.1:7890"
    source.status = STATUS_PROXY_ERROR
    source.consecutive_failures = app_state.cfg.failure_backoff_threshold

    reprobed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        app_state, "ensure_source_proxy",
        lambda sid, fetcher=None, *, force=False: reprobed.append((sid, force)) or None)

    app_state.run_round(skip_ids={source.id}, notify=False)

    assert reprobed == [], "被跳过的来源这一轮没抓取，不该触发重探"
