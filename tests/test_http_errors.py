"""HTTP 错误分类、重试策略与常驻线程池（第二轮审计新增的抓取契约）。"""

from __future__ import annotations

import pytest
import requests

from shunshu import http
from shunshu.http import ConnectionFailureError, Fetcher, FetchError


class _FakeResp:
    """给 Fetcher.session.get 打桩用的最小响应对象。"""

    def __init__(self, status_code: int = 200, ctype: str = "text/html; charset=utf-8",
                 body: bytes = b"<html><body>ok</body></html>", location: str | None = None):
        self.status_code = status_code
        self.headers = {"Content-Type": ctype}
        if location is not None:
            self.headers["Location"] = location
        self.encoding = "utf-8"
        self.body = body
        self.read = False
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size):
        self.read = True
        return [self.body] if self.status_code < 400 else []


def test_4xx_fails_immediately_without_any_retry(monkeypatch):
    """403/404 是站点的明确答复：重试结果不会变，还白白拖慢一轮检查。"""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _FakeResp(404)

    fetcher = Fetcher("ua", retries=3, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://a.com/gone")
    assert "HTTP 404" in str(excinfo.value)
    assert calls == ["https://a.com/gone"], "4xx 必须立刻失败，一次都不许重试"


def test_5xx_is_retried_and_eventually_succeeds(monkeypatch):
    """500/503 多为站点临时抽风：退避重试能救回来，不能一次就记成失败。"""
    states = iter([500, 503, 200])

    def get(url, **kwargs):
        return _FakeResp(next(states))

    fetcher = Fetcher("ua", retries=2, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    assert "ok" in fetcher.fetch_html("https://a.com/")


def test_connection_refused_after_retries_is_connection_failure(monkeypatch):
    """连不上（一个 HTTP 答复都没有）要和「站点答复了错误码」分开，代理探测只认前者切代理。"""

    def get(url, **kwargs):
        raise requests.exceptions.ConnectionError(
            "[WinError 10061] 由于目标计算机积极拒绝，无法连接。")

    fetcher = Fetcher("ua", retries=1, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(ConnectionFailureError):
        fetcher.fetch_html("https://a.com/")


def test_timeout_is_classified_as_connection_failure(monkeypatch):
    def get(url, **kwargs):
        raise requests.exceptions.Timeout("Read timed out")

    fetcher = Fetcher("ua", retries=0, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(ConnectionFailureError):
        fetcher.fetch_html("https://a.com/")


def test_body_read_timeout_is_not_a_connection_failure(monkeypatch):
    """响应头已经拿到，正文超时说明网络路径通，不能据此给站点切代理。"""
    class BodyTimeoutResp(_FakeResp):
        def iter_content(self, chunk_size):
            raise requests.exceptions.ReadTimeout("body timed out")

    fetcher = Fetcher("ua", retries=0, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **k: BodyTimeoutResp())
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://a.com/")
    assert not isinstance(excinfo.value, ConnectionFailureError)


def test_connection_failure_after_body_timeout_uses_the_last_attempt_state(monkeypatch):
    class BodyTimeoutResp(_FakeResp):
        def iter_content(self, chunk_size):
            raise requests.exceptions.ReadTimeout("body timed out")

    calls = 0

    def get(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return BodyTimeoutResp()
        raise requests.exceptions.ConnectionError("[WinError 10061] connection refused")

    fetcher = Fetcher("ua", retries=1, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(ConnectionFailureError):
        fetcher.fetch_html("https://a.com/")
    assert calls == 2


def test_ssl_error_stays_a_plain_fetch_error(monkeypatch):
    """SSL 握手失败时 TCP 其实连上了：不当连接失败，探测也不会据此误切代理。"""

    def get(url, **kwargs):
        raise requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED]")

    fetcher = Fetcher("ua", retries=0, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://a.com/")
    assert not isinstance(excinfo.value, ConnectionFailureError)


def test_target_hostname_with_proxy_word_is_not_a_proxy_failure(monkeypatch):
    fetcher = Fetcher("ua", retries=0, backoff=0)

    def disconnected(url, **kwargs):
        raise requests.exceptions.ConnectionError(f"{url} disconnected")

    monkeypatch.setattr(fetcher.session, "get", disconnected)
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://proxy-book.example.com/", "http://127.0.0.1:7890")
    assert not isinstance(excinfo.value, http.ProxyError)


# ---------- 手动重定向与硬读取时限 ----------

def test_redirect_body_is_not_read_and_final_page_is_capped(monkeypatch):
    redirect = _FakeResp(302, body=b"x" * 10_000, location="/final")
    final = _FakeResp(200, body=b"x" * 101)
    responses = iter([redirect, final])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    fetcher = Fetcher("ua", retries=0, max_bytes=100)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(FetchError, match="100 字节上限"):
        fetcher.fetch_html("https://a.com/start")
    assert [url for url, _ in calls] == ["https://a.com/start", "https://a.com/final"]
    assert all(kwargs["allow_redirects"] is False and kwargs["stream"] is True for _, kwargs in calls)
    assert redirect.closed and not redirect.read, "跳转响应只能关闭，不能读正文"
    assert final.closed and final.read


def test_redirect_location_must_be_http_and_redirect_count_is_limited(monkeypatch):
    calls = []

    def bad_location(url, **kwargs):
        calls.append(url)
        return _FakeResp(302, location="javascript:alert(1)")

    fetcher = Fetcher("ua", retries=2, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", bad_location)
    with pytest.raises(FetchError, match="Location"):
        fetcher.fetch_html("https://a.com/")
    assert calls == ["https://a.com/"], "非法 Location 立即失败，不重试"

    calls.clear()

    def loop(url, **kwargs):
        calls.append(url)
        return _FakeResp(302, location="/again")

    monkeypatch.setattr(fetcher.session, "get", loop)
    with pytest.raises(FetchError, match="30 次上限"):
        fetcher.fetch_html("https://a.com/")
    assert len(calls) == http._MAX_REDIRECTS + 1


def test_redirect_without_location_is_treated_as_final_response(monkeypatch):
    resp = _FakeResp(302)
    fetcher = Fetcher("ua", retries=0)
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **k: resp)
    assert "ok" in fetcher.fetch_html("https://a.com/")
    assert resp.read


def test_final_5xx_after_redirect_still_retries(monkeypatch):
    responses = iter([
        _FakeResp(302, location="/final"), _FakeResp(503),
        _FakeResp(302, location="/final"), _FakeResp(200),
    ])
    fetcher = Fetcher("ua", retries=1, backoff=0)
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **k: next(responses))
    assert "ok" in fetcher.fetch_html("https://a.com/")


def test_blocking_body_read_is_interrupted_by_total_budget(monkeypatch):
    import threading
    import time as real_time

    class BlockingResp(_FakeResp):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def close(self):
            super().close()
            self.release.set()

        def iter_content(self, chunk_size):
            self.read = True
            if not self.release.wait(2):
                raise AssertionError("watchdog 没有关闭响应")
            raise requests.exceptions.ConnectionError("closed by watchdog")

    resp = BlockingResp()
    fetcher = Fetcher("ua", timeout=1, retries=0)
    fetcher.url_budget_seconds = 0.05
    monkeypatch.setattr(fetcher.session, "get", lambda *a, **k: resp)
    started = real_time.monotonic()
    with pytest.raises(FetchError, match="总时限"):
        fetcher.fetch_html("https://slow.example/")
    assert real_time.monotonic() - started < 0.5
    assert resp.closed


def test_blocking_response_headers_are_bounded_by_total_budget(monkeypatch):
    import threading
    import time as real_time

    release = threading.Event()
    finished = threading.Event()

    def blocking_get(url, **kwargs):
        try:
            release.wait(2)
            return _FakeResp()
        finally:
            finished.set()

    fetcher = Fetcher("ua", timeout=1, retries=0)
    fetcher.url_budget_seconds = 0.05
    monkeypatch.setattr(fetcher.session, "get", blocking_get)
    started = real_time.monotonic()
    try:
        with pytest.raises(FetchError, match="总时限"):
            fetcher.fetch_html("https://slow.example/")
        assert real_time.monotonic() - started < 0.5
    finally:
        release.set()
        assert finished.wait(1)


def test_response_returned_after_deadline_is_closed_without_reading(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(http, "time", clock)
    resp = _FakeResp()

    def get(url, **kwargs):
        clock.now += 100
        return resp

    fetcher = Fetcher("ua", retries=0)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(FetchError, match="总时限"):
        fetcher.fetch_html("https://slow.example/")
    assert resp.closed and not resp.read


# ---------- 常驻线程池 ----------

def test_resident_pool_is_reused_and_close_releases():
    """同并发数必须复用同一个池（keep-alive 连接才跨轮活下来），close 后能重建。"""
    fetcher = Fetcher("ua", workers=2, retries=0)
    try:
        pool = fetcher.pool(2)
        assert fetcher.pool(2) is pool, "并发数没变时不许新建池"
        assert list(pool.map(lambda i: i * 2, range(4))) == [0, 2, 4, 6]
    finally:
        fetcher.close()
    # 关掉后再要不报错，会自动建一个新池
    new_pool = fetcher.pool(2)
    assert new_pool is not pool
    fetcher.close()


def test_pool_is_rebuilt_when_workers_change():
    """配置里的并发数改了：旧池直接作废，新池按新容量建。"""
    fetcher = Fetcher("ua", workers=2, retries=0)
    try:
        first = fetcher.pool(2)
        second = fetcher.pool(4)
        assert second is not first and fetcher._pool_workers == 4
    finally:
        fetcher.close()


# ---------- 单页总时限 ----------

class _Clock:
    """假时钟：涓流场景用它把累计耗时按「读到第几块」推进，测试不碰真 sleep。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _TrickleResp:
    """每隔 10 秒吐一小块：每一次读都刚好不触发 socket 超时，但这一页永远读不完。"""

    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    encoding = "utf-8"

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def close(self) -> None:
        pass

    def iter_content(self, chunk_size):
        while True:
            self.clock.now += 10.0
            yield b"<html><body><a href='/1.html'>\xe7\xac\xac1\xe7\xab\xa0</a></body></html>"


def test_default_budget_is_all_attempts_plus_all_backoff():
    """预算要一眼能对上「最坏要多久」：默认 15s×3 次 + 退避 3s+6s = 54s。"""
    assert Fetcher("ua").url_budget_seconds == 54.0
    # 探测用短超时、不重试：取 30 秒下限即可，反正一次尝试就到不了
    assert Fetcher("ua", timeout=6.0, retries=0).url_budget_seconds == 30.0


def test_trickle_site_is_cut_off_by_the_url_budget(monkeypatch):
    """涓流站只有累计预算拦得住：不然一个 URL 能续命到几十秒，把整轮和托盘一起堵死。"""
    clock = _Clock()
    monkeypatch.setattr(http, "time", clock)
    seen: list = []

    def get(url, **kwargs):
        seen.append(kwargs["timeout"])
        return _TrickleResp(clock)

    fetcher = Fetcher("ua", timeout=15, retries=2, backoff=3)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://slow.example.com/1/")
    message = str(excinfo.value)
    assert "总时限" in message and "太慢" in message and "不是连不上" in message
    assert not isinstance(excinfo.value, ConnectionFailureError), "慢不等于连不上，探测不许据此切代理"
    assert seen == [15], "读流里就该掐断，不该再开第二次尝试"
    assert clock.now <= fetcher.url_budget_seconds + 10, "累计耗时被预算钉住，最多多读一块"


def test_exhausted_budget_shrinks_the_attempt_timeout_and_stops(monkeypatch):
    """单次 timeout 只剩多少给多少；预算见底就直接失败，不再开新尝试。"""
    clock = _Clock()
    monkeypatch.setattr(http, "time", clock)
    seen: list = []

    def get(url, **kwargs):
        seen.append(kwargs["timeout"])
        clock.now += 40.0  # 这一趟尝试吃掉 40 秒
        raise requests.exceptions.ConnectionError("[WinError 10060] 连接超时")

    fetcher = Fetcher("ua", timeout=15, retries=2, backoff=3)
    monkeypatch.setattr(fetcher.session, "get", get)
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://slow.example.com/1/")
    assert "总时限" in str(excinfo.value)
    assert seen == [15, 11], "第二次只剩 54-40-3=11 秒给单次 timeout，第三次不许再开"
    assert clock.now == 83.0
