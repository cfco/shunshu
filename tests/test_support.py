from __future__ import annotations

import json

import fixtures
import pytest
import requests
from conftest import FakeFetcher

from shunshu.config import Config, load_config, normalize_text
from shunshu.http import (
    Fetcher,
    FetchError,
    ProxyError,
    assert_http_url,
    decode_body,
    proxy_endpoints,
    proxy_label,
)
from shunshu.models import Novel, Source, host_label, status_label
from shunshu.notify import Notifier
from shunshu.reader import load_toc
from shunshu.store import (
    Paths,
    append_jsonl,
    find_novel,
    load_novels,
    log_filtered,
    now_stamp,
    read_jsonl,
    save_novels,
    source_id_for,
    write_candidates,
)


# ---------- 编码与 URL 校验 ----------

def test_gbk_page_decodes_without_explicit_header():
    text = decode_body(fixtures.gbk_bytes(), None)
    assert "古风测试书" in text and "第1章 风雨如晦" in text


def test_meta_charset_is_respected():
    body = "<html><head><meta charset='gb2312'></head><body>中文</body></html>".encode("gb18030")
    assert "中文" in decode_body(body, "text/html")


def test_broken_bytes_fall_back_instead_of_raising():
    body = b"<html>\xbd\xf1\xb6\xee</html>"
    assert isinstance(decode_body(body, None), str)


def test_assert_http_url_blocks_non_http_schemes():
    assert "ok" in assert_http_url("https://a.com/ok")
    for bad in ["javascript:alert(1)", "file:///C:/x", "data:text/html,x", "//a.com/x", "ftp://a.com"]:
        try:
            assert_http_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"应该拒绝: {bad}")


# ---------- 源级代理 ----------

def test_proxy_endpoints_accept_only_supported_schemes():
    for good in ["http://127.0.0.1:7890", "https://proxy.example.com:8443",
                 "socks5://user:pass@127.0.0.1:7891"]:
        assert proxy_endpoints(good) == {"http": good, "https": good}
    assert proxy_endpoints("") == {}
    assert proxy_endpoints("   ") == {}


def test_proxy_endpoints_reject_unusable_strings_with_clear_reason():
    cases = {
        "127.0.0.1:7890": "只支持",          # 手写时最容易漏掉协议前缀
        "ftp://127.0.0.1:21": "只支持",
        "http://": "只支持",
        "http://127.0.0.1:abc": "端口不是数字",
    }
    for value, hint in cases.items():
        with pytest.raises(ProxyError) as excinfo:
            proxy_endpoints(value)
        assert hint in str(excinfo.value)


def test_proxy_label_never_leaks_credentials():
    label = proxy_label("socks5://reader:secretpwd@127.0.0.1:7890")
    assert label == "socks5://127.0.0.1:7890"


def test_fetcher_ignores_environment_proxy_by_default(monkeypatch):
    """默认直连是硬约束：环境变量里挂着代理也不许读（trust_env=False 且 get 收到空 proxies）。

    代理软件开着「系统代理」或 shell 里有 HTTP_PROXY 时，没手写 proxy 的来源必须照样直连，
    否则国内站会被白白绕一圈，关了代理软件还会全体抓不动。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7890")

    fetcher = Fetcher("test-ua", retries=0)
    session = fetcher.session
    assert session.trust_env is False

    seen: dict = {}

    class FakeResp:
        status_code = 200
        # 故意不带 charset：走 meta/utf-8/gb18030 探测，下面的字节按 gb18030 能解出非空文本
        headers = {"Content-Type": "text/html"}

        def close(self):
            pass

        def iter_content(self, chunk_size):
            return [b"<html><body><a href='/1.html'>\xb5\xda1\xd5\xc2</a></body></html>"]

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return FakeResp()

    monkeypatch.setattr(session, "get", fake_get)
    fetcher.fetch_html("https://book.example.com/")
    assert seen.get("proxies") == {}, "没写 proxy 时不能把环境代理传给 requests"


def test_fetcher_passes_manual_proxy_to_requests(monkeypatch):
    """来源手写了 proxy 时，proxies 必须原样带到这一次请求（只影响这一个来源）。"""
    fetcher = Fetcher("test-ua", retries=0)
    seen: dict = {}

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def close(self):
            pass

        def iter_content(self, chunk_size):
            return [b"<html><body><a href='/1.html'>chapter 1</a></body></html>"]

    monkeypatch.setattr(fetcher.session, "get", lambda url, **kw: (seen.update(kw), FakeResp())[1])
    fetcher.fetch_html("https://book.example.com/", proxy="http://127.0.0.1:7890")
    assert seen.get("proxies") == {"http": "http://127.0.0.1:7890",
                                   "https": "http://127.0.0.1:7890"}


def test_dead_proxy_is_proxy_error_and_is_not_retried(monkeypatch):
    fetcher = Fetcher("test-ua", retries=2, backoff=0)
    seen: list[dict] = []

    def dead_get(url, **kwargs):
        seen.append(kwargs["proxies"])
        inner = requests.exceptions.ConnectionError(
            "Failed to establish a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。")
        raise requests.exceptions.ProxyError(inner)

    monkeypatch.setattr(fetcher.session, "get", dead_get)
    with pytest.raises(ProxyError) as excinfo:
        fetcher.fetch_html("https://book.example.com/12_3456/", "http://127.0.0.1:7890")
    message = str(excinfo.value)
    assert "http://127.0.0.1:7890 走不通" in message
    assert "WinError 10061" in message
    assert seen == [{"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}]


def test_socks_without_pysocks_says_how_to_fix_it(monkeypatch):
    fetcher = Fetcher("test-ua", retries=0, backoff=0)

    def no_socks_support(url, **kwargs):
        raise requests.exceptions.InvalidSchema("Missing dependencies for SOCKS support")

    monkeypatch.setattr(fetcher.session, "get", no_socks_support)
    with pytest.raises(ProxyError) as excinfo:
        fetcher.fetch_html("https://book.example.com/12_3456/", "socks5://127.0.0.1:7890")
    assert "PySocks" in str(excinfo.value)


def test_socks_proxy_refused_still_reads_as_proxy_error(monkeypatch):
    """socks 代理没启动时 requests 只报 ConnectionError，不能混进「网络错误」。"""
    fetcher = Fetcher("test-ua", retries=1, backoff=0)

    class Nested(Exception):
        pass

    def refused(url, **kwargs):
        inner = Nested("SOCKSHTTPSConnection(host='book.example.com', port=443): "
                       "Failed to establish a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。")
        outer = Nested("SOCKSHTTPSConnectionPool(host='book.example.com', port=443): "
                       "Max retries exceeded with url: / (Caused by NewConnectionError)")
        outer.reason = inner
        raise requests.exceptions.ConnectionError(outer)

    monkeypatch.setattr(fetcher.session, "get", refused)
    with pytest.raises(ProxyError) as excinfo:
        fetcher.fetch_html("https://book.example.com/12_3456/", "socks5://127.0.0.1:7890")
    message = str(excinfo.value)
    assert "socks5://127.0.0.1:7890 走不通" in message
    assert "WinError 10061" in message
    assert "ConnectionPool" not in message


def test_site_disconnecting_with_proxy_up_stays_a_network_error(monkeypatch):
    fetcher = Fetcher("test-ua", retries=0, backoff=0)

    def aborted(url, **kwargs):
        raise requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")

    monkeypatch.setattr(fetcher.session, "get", aborted)
    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch_html("https://book.example.com/12_3456/", "http://127.0.0.1:7890")
    assert not isinstance(excinfo.value, ProxyError)


def test_proxy_is_used_for_the_paged_follow_too():
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.paged_first_html()})
    load_toc(fetcher, fixtures.BASE_URL, Config().compiled(), proxy="http://127.0.0.1:7890")
    assert len(fetcher.calls) == 2
    assert set(fetcher.proxies) == {"http://127.0.0.1:7890"}


# ---------- 识别与读取 ----------

def test_reader_skips_missing_last_page_without_failing():
    result = load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.paged_first_html()}),
                      fixtures.BASE_URL, Config().compiled())
    assert len(result.toc.chapters) == 60
    assert result.toc.pages_followed == 1


def test_reader_reports_dropped_ad_links():
    result = load_toc(FakeFetcher({fixtures.BASE_URL: fixtures.ad_heavy_html()}),
                      fixtures.BASE_URL, Config().compiled())
    reasons = {d.reason for d in result.dropped}
    assert "external_host" in reasons and "functional_text" in reasons


# ---------- 存储 ----------

def make_novel() -> Novel:
    source = Source(id=source_id_for(fixtures.BASE_URL), toc_url=fixtures.BASE_URL,
                    site="book.example.com")
    return Novel(id=source.id, title="书", sources=[source], primary_id=source.id)


def test_source_label_keeps_the_subdomain():
    """镜像站常只在子域上差别，来源标签要分得开。"""
    assert host_label("https://m.book.example.com/1/") == "m.book.example.com"
    assert host_label("https://www.other.example.com/1/") == "other.example.com"


def test_novels_json_roundtrip_and_unknown_keys_dropped(paths: Paths):
    novel = make_novel()
    save_novels(paths, [novel])
    raw = json.loads(paths.novels.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    raw["novels"][0]["sources"][0]["未来的字段"] = 1
    paths.novels.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_novels(paths)
    assert loaded[0].title == "书"
    assert loaded[0].sources[0].toc_url == fixtures.BASE_URL
    assert find_novel(loaded, novel.id) is loaded[0]
    assert find_novel(loaded, fixtures.BASE_URL) is loaded[0], "目录页 URL 也能定位到这本书"


def test_novels_write_leaves_no_tmp_file(paths: Paths):
    save_novels(paths, [Novel(id="x", sources=[Source(id="s", toc_url="https://a.com/")])])
    assert not list(paths.root.glob("*.tmp"))


def test_history_jsonl_roundtrip_and_bad_lines_ignored(paths: Paths):
    target = paths.source_history("abc", "def")
    append_jsonl(target, {"kind": "update", "chapters": [{"t": "第1章"}]})
    target.write_text(target.read_text(encoding="utf-8") + "{坏行}\n", encoding="utf-8")
    records = read_jsonl(target)
    assert len(records) == 1 and records[0]["kind"] == "update"


def test_filtered_log_is_readable(paths: Paths):
    source = make_novel().sources[0]
    log_filtered(paths, source, [("ad_text", "澳门赌博", "https://x.com/a"), ("functional_text", "加入书签", "/m")])
    text = paths.filtered_log.read_text(encoding="utf-8")
    assert "ad_text" in text and "澳门赌博" in text and source.id in text


def test_candidates_file_lists_selectors(paths: Paths):
    from shunshu.toc_detect import TocDetectError, parse_page, pick_container

    rules = Config().compiled()
    page = parse_page(fixtures.no_container_html(), fixtures.BASE_URL, rules)
    novel = make_novel()
    source = novel.sources[0]
    try:
        pick_container(page, rules)
        raise AssertionError("应当识别失败")
    except TocDetectError as exc:
        write_candidates(paths, novel, source, exc.candidates, note=str(exc))
    text = paths.source_candidates(source.id).read_text(encoding="utf-8")
    assert "候选" in text and "selector" in text.lower()
    assert "novels.json" in text, "手写 selector 的位置改了，提示要跟着改"


def test_now_stamp_format():
    assert len(now_stamp()) == 19 and now_stamp()[4] == "-"


# ---------- 配置 ----------

def test_config_defaults_written_on_first_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    assert cfg.poll_minutes == Config().poll_minutes and cfg.notified_keys_keep == 500
    assert cfg.poll_minutes in (5, 10, 30, 60), "默认间隔得是托盘菜单里能选的那几个"
    saved = json.loads((tmp_path / "data" / "config.json").read_text(encoding="utf-8"))
    assert saved["poll_minutes"] == cfg.poll_minutes, "首轮写盘的默认值要和内存里一致"
    assert "加入书签" in saved["ad_exact_texts"]


def test_config_partial_file_keeps_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "config.json").write_text(json.dumps({"poll_minutes": 3}), encoding="utf-8")
    cfg = load_config()
    assert cfg.poll_minutes == 3
    assert cfg.ad_exact_texts == Config().ad_exact_texts


def test_invalid_user_regex_is_skipped_not_fatal():
    # 内置 18 条始终合并进来，用户的坏正则跳过、好正则保留：18 + 1
    from shunshu.config import DEFAULT_AD_PATTERNS

    rules = Config(ad_patterns=["(", "记住本站"]).compiled()
    assert len(rules.patterns) == len(DEFAULT_AD_PATTERNS) + 1
    assert any(p.search("欢迎记住本站") for p in rules.patterns)


def test_normalize_text_strips_zero_width():
    assert normalize_text("第 1 章​ 标题") == "第1章标题"


# ---------- 通知 ----------

def test_popup_command_carries_payload_index_and_seconds():
    from shunshu.notify import popup_command
    from shunshu.popup import make_payload

    payload = make_payload("书名<>&", chapters=[{"t": "第1章", "u": "https://x.com/1.html", "read": True}])
    cmd = popup_command(payload, 2, 12.0)
    assert cmd[1:4] == ["-m", "shunshu", "popup"]
    data = json.loads(cmd[cmd.index("--payload") + 1])
    assert data["title"] == "书名<>&" and data["chapters"][0]["read"] is True
    assert cmd[cmd.index("--index") + 1] == "2"
    assert cmd[cmd.index("--seconds") + 1] == "12"


def test_card_lists_five_newest_first_with_read_flags():
    from shunshu.align import match_key
    from shunshu.models import ChapterEntry
    from shunshu.notify import entry_rows

    entries = [ChapterEntry(k=match_key(i, f"第{i}章"), t=f"第{i}章", u=f"https://x.com/{i}.html",
                            s="s2", n=i) for i in range(8, 0, -1)]
    rows = entry_rows(entries, read_keys=[match_key(8, "第8章")], label_of=lambda sid: "镜像站")
    assert [r["t"] for r in rows] == ["第8章", "第7章", "第6章", "第5章", "第4章"]
    assert [r["read"] for r in rows] == [True, False, False, False, False]
    assert rows[0]["k"] == match_key(8, "第8章"), "行里要带跨源身份，已读才认得中另一站的标题"
    assert rows[0]["s"] == "镜像站", "行里要带来源名，多来源才分得清是谁报的"


def test_card_titles_are_simplified():
    from shunshu.models import ChapterEntry
    from shunshu.notify import entry_rows

    rows = entry_rows([ChapterEntry(k="000009|第9章 风雪夜归人", t="第9章 風雪夜歸人",
                                    u="https://x.com/9.html", s="s1", n=9)])
    assert rows[0]["t"] == "第9章 风雪夜归人"
    assert rows[0]["u"] == "https://x.com/9.html", "跳转仍用源站原链接"


def test_card_chapter_click_opens_browser_records_read_and_closes(paths: Paths, monkeypatch):
    """点章节 = 跳浏览器 + 写 read.jsonl + 卡片自己收起（需求：点章节或点关闭都算关掉）。"""
    from conftest import FakeRoot

    from shunshu.align import match_key
    from shunshu.popup import make_payload, open_chapter

    opened = []

    def fake_open(url):
        opened.append(url)
        return True

    monkeypatch.setattr("shunshu.browser.open_in_default_browser", fake_open)
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(paths.root))
    root = FakeRoot()
    payload = make_payload("《书》更新 1 章", novel="b1")
    open_chapter(root, payload, {"t": "第9章", "u": "https://x.com/9.html?from=share",
                                 "k": match_key(9, "第9章"), "read": False})

    assert opened == ["https://x.com/9.html?from=share"]
    record, = read_jsonl(paths.read_log)
    assert record["novel"] == "b1"
    assert record["key"] == match_key(9, "第9章")
    assert record["url"] == "https://x.com/9.html?from=share"
    assert root.destroyed, "点完章节卡片要收起"


def test_card_does_not_mark_read_when_browser_fails(paths: Paths, monkeypatch):
    from conftest import FakeRoot

    from shunshu.popup import make_payload, open_chapter

    monkeypatch.setattr("shunshu.browser.open_in_default_browser", lambda url: False)
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(paths.root))
    root = FakeRoot()
    open_chapter(root, make_payload("《书》", novel="b1"), {"t": "第9章", "u": "https://x.com/9.html",
                                                            "k": "000009|第9章"})
    assert paths.read_log.exists() is False, "没打开成功就不能记成已读"
    assert root.destroyed


def test_notifier_records_alerts_log(paths: Paths, monkeypatch):
    calls = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: calls.append((payload, index)) or True)
    notifier = Notifier(alerts_path=paths.alerts_log, enabled=True)
    assert notifier.show("书", ["第9章 新"]) is True
    assert calls[0][0]["title"] == "书" and calls[0][0]["lines"] == ["第9章 新"]
    text = paths.alerts_log.read_text(encoding="utf-8")
    assert "第9章 新" in text


def test_notifier_can_be_silenced(paths: Paths, monkeypatch):
    calls = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: calls.append(payload) or True)
    notifier = Notifier(alerts_path=paths.alerts_log, enabled=False)
    assert notifier.show("书", ["第9章 新"]) is False
    assert calls == []
    assert "第9章 新" in paths.alerts_log.read_text(encoding="utf-8")


def test_notifier_rejects_non_http_chapter_url(paths: Paths, monkeypatch):
    from shunshu.models import ChapterEntry, NovelAlert

    calls = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: calls.append(payload) or True)
    source = Source(id="b1", toc_url="https://x.com/1/", site="x.com")
    entry = ChapterEntry(k="000009|第9章", t="第9章", u="javascript:alert(1)", s="b1")
    novel = Novel(id="b1", title="书", sources=[source], primary_id="b1", chapter_index=[entry])
    notifier = Notifier(alerts_path=paths.alerts_log, enabled=True)

    assert notifier.notify_alert(NovelAlert(novel=novel, entries=[entry])) is True
    assert "javascript" not in paths.alerts_log.read_text(encoding="utf-8")


def test_second_instance_backs_off_with_a_card(tmp_path, monkeypatch):
    """重复启动不该多出第二个托盘图标：拿不到互斥体时 run() 提示完就退出。"""
    import pytest

    from shunshu import instance, tray
    from shunshu.app import AppState
    from shunshu.store import Paths

    assert instance.acquire_singleton("Local\\shunshu-test-single") is True
    assert instance.acquire_singleton("Local\\shunshu-test-single") is True, "同一个进程不该把自己当成第二个实例"

    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0, seconds=12.0: cards.append(payload) or True)
    monkeypatch.setattr(tray, "acquire_singleton", lambda: False)
    app = tray.TrayApp(AppState(Paths(tmp_path / "data")))
    app.run()
    assert app.icon is None, "第二个实例不能建托盘图标"
    assert cards and "已经在运行" in cards[0]["title"]


def test_status_labels_are_chinese():
    assert status_label("updated") == "有更新"
    assert status_label("unknown_x") == "unknown_x"


def test_fetcher_switches_tls_trust_to_the_system_store(monkeypatch):
    """验 TLS 要像浏览器一样会补中间证书：只发叶子证书的站才打得开。"""
    import sys
    from types import SimpleNamespace

    from shunshu import http

    calls = []
    monkeypatch.setattr(http, "_TRUST_STORE_DONE", False)
    monkeypatch.setitem(sys.modules, "truststore", SimpleNamespace(inject_into_ssl=lambda: calls.append(1)))
    http.Fetcher("ua")
    assert calls == [1]


def test_a_failing_trust_store_hook_leaves_the_fetcher_working(monkeypatch):
    import sys
    from types import SimpleNamespace

    from shunshu import http

    def boom():
        raise RuntimeError("系统库用不了")

    monkeypatch.setattr(http, "_TRUST_STORE_DONE", False)
    monkeypatch.setitem(sys.modules, "truststore", SimpleNamespace(inject_into_ssl=boom))
    assert http.Fetcher("ua") is not None, "注入失败就退回 certifi，不能把抓取器一起弄挂"


def test_popup_command_shape_follows_the_runtime(monkeypatch, tmp_path):
    """打包态拼 [exe, popup]，源码态拼 [python, -m, shunshu, popup]：拼错就永远弹不出卡。"""
    import os
    import sys

    from shunshu import notify

    exe = tmp_path / "shunshu.exe"
    exe.write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    argv = notify.popup_command({"title": "t"}, 2, 12.0)
    assert os.path.normcase(argv[0]) == os.path.normcase(str(exe))
    assert argv[1] == "popup" and "-m" not in argv
    assert argv[argv.index("--index") + 1] == "2"

    interpreter = tmp_path / "python.exe"
    interpreter.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "__main__.py")])
    assert notify.popup_command({"title": "t"}, 0, 12.0)[:4] ==         [str(interpreter), "-m", "shunshu", "popup"]


def test_failed_card_spawn_is_recorded_with_a_reason(tmp_path, monkeypatch):
    """卡片弹不出来不能静默：现场只有 alerts.log 一份东西可看。"""
    from shunshu import notify

    def failing(payload, index=0, seconds=12.0):
        notify.LAST_SPAWN_ERROR = "起进程失败 FileNotFoundError: [WinError 2]"
        return False

    monkeypatch.setattr(notify, "spawn_card", failing)
    path = tmp_path / "alerts.log"
    assert notify.Notifier(alerts_path=path).show("《书》更新 1 章", ["第9章 新"]) is False
    text = path.read_text(encoding="utf-8")
    assert "卡片没弹出" in text and "FileNotFoundError" in text
