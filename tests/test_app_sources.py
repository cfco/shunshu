"""书架与来源维护、托盘来源菜单、CLI 入口。"""

from __future__ import annotations

import json

import fixtures
import pytest
from conftest import FakeFetcher, FakeProc

from shunshu.app import AppState, DuplicateSource
from shunshu.cli import build_parser, main
from shunshu.proxy_detect import ProbeResult
from shunshu.store import source_id_for


# ---------- 书架读写 ----------

def test_add_novel_persists_and_rejects_duplicates(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.save_novels()
    assert novel.id == source_id_for(fixtures.BASE_URL)
    assert [n.id for n in AppState(app_state.paths).novels] == [novel.id]
    with pytest.raises(DuplicateSource):
        app_state.add_novel(fixtures.BASE_URL)


def test_add_novel_rejects_non_http_url(app_state: AppState):
    with pytest.raises(ValueError):
        app_state.add_novel("javascript:alert(1)")


def test_novels_json_is_hand_editable_then_reloaded(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    app_state.save_novels()
    raw = json.loads(app_state.paths.novels.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    source = raw["novels"][0]["sources"][0]
    source["selector"] = "dl.list"
    source["keep_keys"] = ["https://book.example.com/9999/200.html"]
    source["proxy"] = "http://127.0.0.1:7890"
    app_state.paths.novels.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    app_state.reload()
    loaded = app_state.novels[0].sources[0]
    assert loaded.selector == "dl.list"
    assert loaded.keep_keys == ["https://book.example.com/9999/200.html"]
    assert loaded.proxy == "http://127.0.0.1:7890"


def test_missing_or_null_proxy_loads_as_no_proxy(app_state: AppState):
    app_state.add_novel(fixtures.BASE_URL)
    app_state.save_novels()
    raw = json.loads(app_state.paths.novels.read_text(encoding="utf-8"))
    raw["novels"][0]["sources"][0]["proxy"] = None
    app_state.paths.novels.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    app_state.reload()
    assert app_state.novels[0].sources[0].proxy == ""


# ---------- 来源维护 ----------

def test_add_source_reuses_one_novel_and_keeps_both_sources(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    url_b = "https://other.example.com/12_3456/"
    source = app_state.add_source(novel.id, url_b)

    assert len(app_state.novels) == 1
    assert [s.toc_url for s in novel.sources] == [fixtures.BASE_URL, url_b]
    assert novel.primary_id == novel.sources[0].id, "主源默认是最先加的那个"
    assert novel.source(source.id) is source
    assert novel.source(url_b) is source, "目录页 URL 也能定位来源"
    assert app_state.novel(url_b) is novel, "用任一来源的 URL 都能找到这本书"


def test_add_source_rejects_url_already_on_another_book(app_state: AppState):
    other = app_state.add_novel("https://book.example.com/88_99/")
    novel = app_state.add_novel(fixtures.BASE_URL)
    with pytest.raises(DuplicateSource):
        app_state.add_source(novel.id, other.sources[0].toc_url)


def test_remove_last_source_drops_the_whole_novel(app_state: AppState):
    novel = app_state.add_novel(fixtures.BASE_URL)
    url_b = "https://other.example.com/12_3456/"
    second = app_state.add_source(novel.id, url_b)

    assert app_state.remove_source(novel.id, second.id) is second
    assert len(novel.sources) == 1
    assert app_state.novels[0].primary_id == novel.sources[0].id

    last = novel.sources[0]
    assert app_state.remove_source(novel.id, last.id) is last
    assert app_state.novels == []


def test_history_merges_records_from_every_source(app_state: AppState):
    url_b = "https://mirror-novel.net/12_3456/"
    fetcher = FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(), url_b: fixtures.normal_html()})
    novel = app_state.add_novel(fixtures.BASE_URL)
    second = app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(fetcher)
    app_state.run_round(notify=False)

    fetcher.add(fixtures.BASE_URL, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 甲站更"]))
    fetcher.add(url_b, fixtures.normal_html([*fixtures.CHAPTERS, "第61章 乙站更"]))
    app_state.run_round(notify=False)

    records = app_state.history(novel.id)
    assert len(records) == 4, "两个来源各自的首轮和更新轮都要在这张清单里"
    assert {r["site"] for r in records} == {"book.example.com", "mirror-novel.net"}
    updates = [r for r in records if r["kind"] == "update"]
    assert {u["chapters"][0]["t"] for u in updates} == {"第61章 甲站更", "第61章 乙站更"}
    assert records == sorted(records, key=lambda r: r["ts"], reverse=True)
    assert app_state.paths.source_history(novel.id, second.id).exists()


# ---------- 入口 ----------

def test_cli_parser_defaults_to_run():
    args = build_parser().parse_args([])
    assert args.command is None


def test_cli_rejects_the_removed_subcommands(tmp_path, monkeypatch, capsys):
    """命令行不再对外提供操作：argparse 直接拒掉，也不去偷偷建数据目录。"""
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as exit_info:
        main(["list"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_popup_subcommand_still_accepts_what_notify_builds():
    """弹卡片是主程序内部调用：入口收窄后这条参数还得对得上。"""
    from shunshu.notify import popup_command
    from shunshu.popup import make_payload

    argv = popup_command(make_payload("标题", lines=["一行"]), 2)[3:]
    args = build_parser().parse_args(argv)
    assert args.command == "popup" and args.index == 2
    assert json.loads(args.payload)["title"] == "标题"


def _broken_state(*_args, **_kwargs):
    raise OSError("模拟起不来：数据目录是只读的")


def test_startup_crash_leaves_a_traceback_and_a_card(tmp_path, monkeypatch):
    """编译版没有控制台（--windows-console-mode=disable），起不来时 traceback 必须落到
    data/shunshu.log 上，并尽力弹一张卡说一声，否则现场只剩「双击没反应」。"""
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("shunshu.app.AppState", _broken_state)

    assert main(["run"]) == 1

    log = (tmp_path / "data" / "shunshu.log").read_text(encoding="utf-8")
    assert "启动失败" in log and "模拟起不来" in log and "Traceback" in log
    alerts = (tmp_path / "data" / "alerts.log").read_text(encoding="utf-8")
    assert "顺手启动失败" in alerts


def test_startup_crash_falls_back_to_temp_log_when_data_is_unwritable(tmp_path, monkeypatch):
    """崩溃本身可能就是 data/ 写不动引起的：这时 traceback 退到系统临时目录，不能一起丢掉。"""
    data = tmp_path / "data"
    (data / "shunshu.log").mkdir(parents=True)  # 这个位置上是个目录，追加必然失败
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(data))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("shunshu.app.AppState", _broken_state)

    assert main(["run"]) == 1

    crash = (tmp_path / "shunshu-crash.log").read_text(encoding="utf-8")
    assert "启动失败" in crash and "模拟起不来" in crash


# ---------- 托盘来源展示与维护 ----------

def test_tray_source_line_shows_proxy_without_credentials(app_state: AppState):
    from shunshu.tray import TrayApp

    novel = app_state.add_novel(fixtures.BASE_URL)
    source = novel.sources[0]
    source.proxy = "socks5://reader:secretpwd@127.0.0.1:7890"
    app = TrayApp(app_state)

    labels = [str(item.text) for item in app._novel_submenu(novel.id)]
    assert any("来源 book.example.com" in t and "代理 socks5://127.0.0.1:7890" in t for t in labels)
    assert "secretpwd" not in " ".join(labels)


def test_source_maintenance_lives_on_app_state(app_state: AppState):
    """主源、暂停、删除都记在 AppState 上并落盘，托盘菜单只是它的一张皮。"""
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)

    assert app_state.set_primary(novel.id, mirror.id) is mirror
    assert novel.primary_id == mirror.id
    assert AppState(app_state.paths).novels[0].primary_id == mirror.id, "换主源要落盘"

    assert app_state.set_source_enabled(novel.id, mirror.id, False) is mirror
    assert mirror.enabled is False
    assert app_state.set_source_enabled(novel.id, mirror.id, True) is mirror
    assert mirror.enabled is True

    assert app_state.set_primary(novel.id, "没有这个来源") is None
    assert app_state.set_source_enabled(novel.id, "没有这个来源", True) is None

    assert app_state.remove_source(novel.id, mirror.id) is mirror
    assert novel.primary_id == novel.sources[0].id, "主源被删后要落到还在的来源上"
    assert app_state.remove_source(novel.id, novel.sources[0].id) is not None
    assert app_state.novels == [], "最后一个来源移走，整本一起下架"


def test_tray_primary_and_pause_actions_take_effect(app_state: AppState, monkeypatch):
    from shunshu.tray import TrayApp

    monkeypatch.setattr("shunshu.notify.spawn_card", lambda payload, index=0: ("", FakeProc()))
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app = TrayApp(app_state)

    app.act_set_primary(novel.id, mirror.id)
    assert novel.primary_id == mirror.id
    app.act_set_primary(novel.id, "没有这个来源")
    assert novel.primary_id == mirror.id, "找不到来源时不该把主源改坏"

    app.act_toggle_source(novel.id, mirror.id)
    assert mirror.enabled is False
    app.act_toggle_source(novel.id, mirror.id)
    assert mirror.enabled is True
    assert AppState(app_state.paths).novels[0].sources[1].enabled is True, "暂停状态也要落盘"


def test_tray_new_source_receipt_names_that_source(app_state: AppState, monkeypatch):
    """新来源首轮只建基线不弹卡，所以回执必须自己说清挂上了谁、抓到什么。"""
    from shunshu.tray import TrayApp

    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0: cards.append(payload) or ("", FakeProc()))
    # 本用例只验回执文案；代理探测由 test_probe_flow 专测，这里直接判「探测通过」
    monkeypatch.setattr(app_state, "ensure_source_proxy",
                        lambda sid, fetcher=None: ProbeResult(proxy=""))
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                                       url_b: fixtures.normal_html(list(fixtures.CHAPTERS))}))
    TrayApp(app_state)._check_new_source(novel.id, mirror.id)
    receipt = next(c for c in cards if "已挂上来源" in c["title"])
    assert "mirror-novel.net" in receipt["title"]
    assert "首次记录" in " ".join(receipt["lines"]), "首轮建基线要说清抓到什么，别让人以为没生效"


def test_tray_source_submenu_lists_actions(app_state: AppState):
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app = TrayApp(app_state)

    items = list(app._source_submenu(novel.id, mirror.id))
    labels = [str(i.text) for i in items]
    assert labels[0] == url_b, "第一行就是目录页网址，方便核对挂没挂对"
    assert "设为主源" in labels and "暂停此源" in labels and "删除此源" in labels
    assert next(i for i in items if str(i.text) == "设为主源").enabled is True

    primary_items = list(app._source_submenu(novel.id, novel.sources[0].id))
    assert next(i for i in primary_items if str(i.text) == "设为主源").enabled is False

    rows = [str(i.text) for i in app._novel_submenu(novel.id)]
    assert any(t.startswith("来源 mirror-novel.net") and "（主源）" not in t for t in rows)
    assert any(t.startswith("来源 book.example.com") and "（主源）" in t for t in rows)
    assert "添加来源到本书（从剪贴板）" in rows


def test_tray_delete_source_needs_two_clicks(app_state: AppState, monkeypatch):
    from shunshu.tray import TrayApp

    monkeypatch.setattr("shunshu.notify.spawn_card", lambda payload, index=0: ("", FakeProc()))
    url_b = "https://mirror-novel.net/12_3456/"
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app = TrayApp(app_state)

    app.act_delete_source(novel.id, mirror.id)
    assert len(novel.sources) == 2, "第一次点只是武装，不能真删"
    app.act_delete_source(novel.id, mirror.id)
    assert [s.id for s in novel.sources] == [novel.sources[0].id]


def test_tray_add_source_from_clipboard_then_baselines_quietly(app_state: AppState, monkeypatch):
    """剪贴板里是另一个站的目录页：挂上后首轮建基线，不弹「已添加」卡，只给一句回执。"""
    from shunshu import tray
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    cards = []
    monkeypatch.setattr("shunshu.notify.spawn_card",
                        lambda payload, index=0: cards.append(payload) or ("", FakeProc()))
    monkeypatch.setattr(tray, "read_text", lambda: f"这个站更快 {url_b}")
    novel = app_state.add_novel(fixtures.BASE_URL)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                                       url_b: fixtures.normal_html(list(fixtures.CHAPTERS))}))
    app = TrayApp(app_state)
    app._check_new_source = lambda novel_id, source_id: None

    app.act_add_source_from_clipboard(novel.id)
    assert [s.toc_url for s in novel.sources] == [fixtures.BASE_URL, url_b]
    assert cards == [], "追加来源不该再弹一张「已添加」卡"

    app.act_add_source_from_clipboard(novel.id)
    assert len(novel.sources) == 2, "同一网址不能挂两次"
    assert any("已在书架里" in str(c["lines"]) + c["title"] for c in cards)


def test_displayed_titles_are_simplified_everywhere(app_state: AppState):
    """显示统一简体：来源行与摘要行里都不能冒出繁体（网页本身仍是繁体，跳转地址不变）。"""
    from shunshu.tray import TrayApp

    url_b = "https://mirror-novel.net/12_3456/"
    traditional = [f"第{i}章 測試章節{i:04d}" for i in range(1, 61)] + ["第61章 風雪夜歸人"]
    novel = app_state.add_novel(fixtures.BASE_URL)
    mirror = app_state.add_source(novel.id, url_b)
    app_state.set_fetcher(FakeFetcher({fixtures.BASE_URL: fixtures.normal_html(),
                                       url_b: fixtures.normal_html(traditional)}))
    outcomes = app_state.check_one(novel.id, notify=False)

    summaries = "".join(o.summary() for o in outcomes)
    assert "風雪" not in summaries and "測試" not in summaries
    assert "第61章 风雪夜归人" in summaries
    assert mirror.latest_title == "第61章 風雪夜歸人", "存下来的是源站原文，转简体只发生在显示那一步"
    rows = [str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, mirror.id)]
    assert any("最后一章《第61章 风雪夜归人》" in t for t in rows)
    assert mirror.toc_url == url_b


def test_tray_source_row_shows_which_proxy_actually_applies(app_state: AppState, monkeypatch):
    """代理只能在 json 里写：没写就是直连（不读系统/环境代理），写了显示手写覆盖且不落凭据。"""
    from shunshu.tray import TrayApp

    novel = app_state.add_novel(fixtures.BASE_URL)
    source = novel.sources[0]

    # 没写 proxy：无论系统代理是否开着，抓取都直连，菜单如实写「直连」
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    rows = " | ".join(str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, source.id))
    assert "代理 直连（不走系统代理）" in rows

    source.proxy = "socks5://10.0.0.9:1080"
    rows = " | ".join(str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, source.id))
    assert "代理 socks5://10.0.0.9:1080（手写覆盖）" in rows

    # 手写代理带账号密码时，菜单里只能看到主机和端口，凭据绝不显示
    source.proxy = "http://me:secret-pw@127.0.0.1:7890"
    rows = " | ".join(str(i.text) for i in TrayApp(app_state)._source_submenu(novel.id, source.id))
    assert "代理 http://127.0.0.1:7890（手写覆盖）" in rows
    assert "secret-pw" not in rows and "me:@" not in rows, "代理凭据不能出现在菜单里"
