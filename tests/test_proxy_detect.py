"""代理自动探测的纯单元测试：注册表值解析、候选收集、TCP 预探活、先直连后代理流程。

不联网、不读真实注册表、不真连端口：注册表值与候选列表都 monkeypatch 掉，
TCP 层用 socket.create_connection 假对象替换。
"""

from __future__ import annotations

import socket

from shunshu.http import ConnectionFailureError, FetchError, ProxyError
from shunshu import proxy_detect
from shunshu.proxy_detect import (
    ProbeResult,
    candidate_proxies,
    parse_proxy_server,
    probe_url,
    tcp_reachable,
)


# ---------- 注册表 ProxyServer 解析 ----------

def test_parse_proxy_server_empty():
    assert parse_proxy_server(None) == []
    assert parse_proxy_server("") == []
    assert parse_proxy_server("   ") == []


def test_parse_proxy_server_bare_host_port_gets_http_scheme():
    assert parse_proxy_server("127.0.0.1:7890") == ["http://127.0.0.1:7890"]


def test_parse_proxy_server_prefixed_mapping_dedupes_same_endpoint_prefers_https():
    raw = "http=127.0.0.1:7890;https=127.0.0.1:7890;socks=127.0.0.1:1080"
    # 同一 host:port 的 http= / https= 是同一个端口，只留 https 形式（目录页多为 https）；
    # socks= 按 requests 的要求补成 socks5://
    assert parse_proxy_server(raw) == [
        "https://127.0.0.1:7890",
        "socks5://127.0.0.1:1080",
    ]


def test_parse_proxy_server_keeps_full_url_and_dedupes():
    raw = "http://user:pw@127.0.0.1:7890,127.0.0.1:7890; broken"
    out = parse_proxy_server(raw)
    assert out[0] == "http://user:pw@127.0.0.1:7890", "带凭据的完整 URL 要原样保留"
    # 同一 host:port 的裸写法与带凭据写法是同一个端点，去重时保留先出现的那个
    assert "http://127.0.0.1:7890" not in out
    assert "http://broken" in out, "不同端点（这里落到默认 80）不能被误去重"
    assert len(out) == 2


def test_parse_proxy_server_drops_pieces_without_host():
    assert parse_proxy_server("=8080; ;xxx") == ["http://xxx"]


# ---------- 候选收集 ----------

def test_registry_proxy_comes_before_default_ports(monkeypatch):
    monkeypatch.setattr(proxy_detect, "registry_proxy_server", lambda: "127.0.0.1:7890")
    candidates = candidate_proxies()
    assert candidates[0] == "http://127.0.0.1:7890", "系统代理是用户最可能在用的，必须排第一"
    assert len(candidates) == len(set(candidates))


def test_candidates_fall_back_to_known_local_ports(monkeypatch):
    monkeypatch.setattr(proxy_detect, "registry_proxy_server", lambda: None)
    candidates = candidate_proxies()
    assert "http://127.0.0.1:7890" in candidates  # Clash
    assert "http://127.0.0.1:10809" in candidates  # v2rayN HTTP
    assert "socks5://127.0.0.1:10808" in candidates  # v2rayN SOCKS
    assert all(("127.0.0.1" in c) for c in candidates), "只允许探测本机代理"


# ---------- TCP 预探活 ----------

class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_tcp_reachable_true_when_port_open(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: _FakeSocket())
    assert tcp_reachable("http://127.0.0.1:7890") is True


def test_tcp_reachable_false_when_port_closed(monkeypatch):
    def refuse(addr, timeout):
        raise OSError("WinError 10061 拒绝连接")

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert tcp_reachable("http://127.0.0.1:7890") is False


def test_tcp_reachable_skips_probe_for_non_loopback(monkeypatch):
    def boom(addr, timeout):
        raise AssertionError("非回环地址不该做预探活")

    monkeypatch.setattr(socket, "create_connection", boom)
    assert tcp_reachable("http://10.0.0.9:8080") is True


# ---------- 先直连后代理 ----------

class _ScriptedFetcher:
    """按 proxy 参数脚本化：记录每次尝试，直连/不同代理可以给不同结果。"""

    def __init__(self, decide):
        self.decide = decide
        self.tried: list[str] = []

    def fetch_html(self, url: str, proxy: str = "") -> str:
        self.tried.append(proxy)
        result = self.decide(proxy)
        if isinstance(result, Exception):
            raise result
        return result


def test_probe_returns_direct_result_when_direct_works(monkeypatch):
    def decide(proxy):
        return "<html>目录页</html>" if proxy == "" else FetchError("直连能通就不该试代理")

    fetcher = _ScriptedFetcher(decide)
    result = probe_url(fetcher, "https://book.example.com/")
    assert result == ProbeResult(proxy="", html="<html>目录页</html>")
    assert fetcher.tried == [""], "直连成功时不许再试任何代理"


def test_probe_direct_http_error_still_counts_as_direct(monkeypatch):
    """直连拿到的是 HTTP 答复（哪怕 503/403/空页）：路径就是通的，不许误切代理。"""

    def decide(proxy):
        return FetchError("HTTP 503") if proxy == "" else AssertionError("不该试代理")

    fetcher = _ScriptedFetcher(decide)
    result = probe_url(fetcher, "https://book.example.com/")
    assert result == ProbeResult(proxy="", html="")
    assert fetcher.tried == [""]


def test_probe_falls_back_to_first_working_local_proxy(monkeypatch):
    monkeypatch.setattr(proxy_detect, "candidate_proxies",
                        lambda: ["http://127.0.0.1:7897", "http://127.0.0.1:7890"])
    monkeypatch.setattr(proxy_detect, "tcp_reachable", lambda url, timeout=1.0: True)

    def decide(proxy):
        if proxy == "":
            return ConnectionFailureError("直连超时")
        if proxy == "http://127.0.0.1:7897":
            return ProxyError("这个端口虽然开着但不是代理")
        return "<html>目录页</html>"

    fetcher = _ScriptedFetcher(decide)
    result = probe_url(fetcher, "https://m.proxy-book.example/123/")
    assert result == ProbeResult(proxy="http://127.0.0.1:7890", html="<html>目录页</html>"), \
        "代理成功时要把抓回的 HTML 一并带回，首轮建档直接复用"
    assert fetcher.tried == ["", "http://127.0.0.1:7897", "http://127.0.0.1:7890"]


def test_probe_via_proxy_with_http_error_still_picks_that_proxy(monkeypatch):
    """经代理能拿到 HTTP 答复（内容层失败）：这条代理路径可用，照样选中。"""
    monkeypatch.setattr(proxy_detect, "candidate_proxies",
                        lambda: ["http://127.0.0.1:7890"])
    monkeypatch.setattr(proxy_detect, "tcp_reachable", lambda url, timeout=1.0: True)

    def decide(proxy):
        if proxy == "":
            return ConnectionFailureError("直连拒绝")
        return FetchError("HTTP 403")

    fetcher = _ScriptedFetcher(decide)
    assert probe_url(fetcher, "https://book.example.com/") == ProbeResult("http://127.0.0.1:7890", "")


def test_probe_returns_none_when_everything_fails(monkeypatch):
    monkeypatch.setattr(proxy_detect, "candidate_proxies",
                        lambda: ["http://127.0.0.1:7890", "socks5://127.0.0.1:1080"])
    # 两个端口都没监听：TCP 预探活就该全刷掉，一次 HTTP 尝试都不发
    monkeypatch.setattr(proxy_detect, "tcp_reachable", lambda url, timeout=1.0: False)
    fetcher = _ScriptedFetcher(
        lambda proxy: ConnectionFailureError("unreachable") if proxy == "" else FetchError("不该走到"))
    assert probe_url(fetcher, "https://dead.example/") is None
    assert fetcher.tried == [""], "端口没监听的候选不该进入 HTTP 探测"


def test_probe_without_any_candidate_returns_none(monkeypatch):
    monkeypatch.setattr(proxy_detect, "candidate_proxies", lambda: [])
    fetcher = _ScriptedFetcher(
        lambda proxy: ConnectionFailureError("直连不通且没有任何代理候选"))
    assert probe_url(fetcher, "https://dead.example/") is None


def test_make_probe_fetcher_is_short_timeout_and_no_retry():
    fetcher = proxy_detect.make_probe_fetcher("ua-test", max_bytes=12345)
    assert fetcher.timeout == proxy_detect.PROBE_TIMEOUT
    assert fetcher.retries == 0, "探测不能重试，否则添加一本书可能卡上几十秒"
    assert fetcher.max_bytes == 12345
