"""数据层韧性：日志按行轮转、坏 JSON 隔离备份、内置名单合并、新配置项。"""

from __future__ import annotations

import json

import fixtures

from shunshu.config import (
    DEFAULT_AD_EXACT,
    DEFAULT_AD_PATTERNS,
    DEFAULT_TITLE_SUFFIXES,
    Config,
    Rules,
    load_config,
    normalize_text,
)
from shunshu.models import Novel, Source
from shunshu.store import Paths, load_novels, save_novels, source_id_for


def _make_novel() -> Novel:
    source = Source(id=source_id_for(fixtures.BASE_URL), toc_url=fixtures.BASE_URL,
                    site="book.example.com")
    return Novel(id=source.id, title="书", sources=[source], primary_id=source.id)


# ---------- 日志轮转 ----------

def test_append_text_rotates_on_line_boundaries(tmp_path, monkeypatch):
    """日志超 2MB 就只留尾部：必须落在行边界上，绝不能留下半行坏数据。"""
    from shunshu import store

    monkeypatch.setattr(store, "_ROTATE_BYTES", 120)
    monkeypatch.setattr(store, "_ROTATE_KEEP_RATIO", 0.75)
    log = tmp_path / "run.log"
    for i in range(40):
        store.append_text(log, f"line-{i:02d}-" + "x" * 20)
    text = log.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines and all(line.startswith("line-") for line in lines), "裁完不能有半行"
    assert "line-00" not in text, "开头的旧行应被裁掉"
    assert lines[-1] == "line-39-" + "x" * 20, "最新一行必须保留"
    assert log.stat().st_size <= 120 + 40


# ---------- JSON 损坏隔离 ----------

def test_broken_novels_json_is_quarantined_and_emptied(paths: Paths):
    """手滑把 novels.json 改成非法 JSON：原文件留 .bak 现场，书架按空启动，不崩不丢现场。"""
    from shunshu import store

    store.LAST_LOAD_WARNINGS.clear()
    paths.novels.write_text("{这不是合法的 json", encoding="utf-8")
    assert load_novels(paths) == []
    backups = list(paths.root.glob("novels.json.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8").startswith("{")
    assert store.LAST_LOAD_WARNINGS, "降级原因要进 LAST_LOAD_WARNINGS 给托盘弹卡"
    # 隔离后位置腾出来，正常保存/读取立刻恢复
    save_novels(paths, [_make_novel()])
    assert len(load_novels(paths)) == 1


def test_non_object_novel_entries_are_skipped_not_fatal(paths: Paths):
    """书架里单条写坏不连累整架：坏条目跳过 + 警告，好条目照常加载。"""
    from shunshu import store

    store.LAST_LOAD_WARNINGS.clear()
    good = _make_novel().to_dict()
    paths.novels.write_text(json.dumps({"version": 2, "novels": [123, good, "坏字符串"]},
                                       ensure_ascii=False), encoding="utf-8")
    novels = load_novels(paths)
    assert len(novels) == 1 and novels[0].title == "书"
    assert store.LAST_LOAD_WARNINGS, "跳过的条目要有说明"


def test_broken_nested_entries_do_not_block_startup(paths: Paths):
    """「下一级」写坏同样不能崩：少一对花括号的 sources、缺 id 的那本，都只是这一条不好使。

    Novel/Source.from_dict 以前会拿字符串当字典读（AttributeError）或因缺 id 抛
    TypeError，异常一路冒出 AppState() —— 启动.bat 没有控制台，现象就是托盘根本不出现。
    """
    from shunshu import store

    store.LAST_LOAD_WARNINGS.clear()
    good = _make_novel().to_dict()
    paths.novels.write_text(json.dumps({"version": 2, "novels": [
        {"id": "n1", "title": "花括号掉了", "primary_id": "n1",
         "sources": ["https://book.example.com/12_3456/"]},
        {"title": "没有 id 的一本", "sources": []},
        good,
    ]}, ensure_ascii=False), encoding="utf-8")

    novels = load_novels(paths)
    assert [n.id for n in novels] == ["n1", good["id"]], "坏的那本跳过，其余照常"
    assert novels[0].sources == []
    warnings = "；".join(store.LAST_LOAD_WARNINGS)
    assert "sources 里没有一条能用" in warnings
    assert "没有 id" in warnings


def test_source_field_types_are_normalized(paths: Paths):
    """手改的名单与代理字段类型不对时归一成能用的形状，别把 TypeError 留给轮询。"""
    paths.novels.write_text(json.dumps({"version": 2, "novels": [{
        "id": "n1", "title": "书", "primary_id": "s1",
        "read_keys": None, "alerted_keys": {"a": 1},
        "chapter_index": [{"t": "没有 k 的行"}, {"k": "000123|甲", "t": "第1章 甲",
                           "u": "https://x.example.com/1.html"}],
        "sources": [{"toc_url": "https://x.example.com/1/", "proxy": 7890,
                     "keep_keys": "https://x.example.com/2.html", "notified_keys": None}],
    }]}, ensure_ascii=False), encoding="utf-8")

    novel = load_novels(paths)[0]
    source = novel.sources[0]
    assert novel.read_keys == [] and novel.alerted_keys == []
    assert [e.k for e in novel.chapter_index] == ["000123|甲"]
    assert source.proxy == "7890" and source.keep_keys == ["https://x.example.com/2.html"]
    assert source.notified_keys == [] and source.known_keys() == set()
    # id 是从目录页 URL 补算的，来源照样能用
    assert source.id == source_id_for("https://x.example.com/1/")


def test_sources_without_toc_url_are_dropped_with_warning(paths: Paths):
    """没有目录页网址的来源抓不了：丢掉并说明，别让 urlsplit 在半路上炸。"""
    from shunshu import store

    store.LAST_LOAD_WARNINGS.clear()
    paths.novels.write_text(json.dumps({"version": 2, "novels": [{
        "id": "n1", "title": "书", "primary_id": "s1",
        "sources": [{"id": "bad", "proxy": ""}, {"id": "s1", "toc_url": fixtures.BASE_URL}],
    }]}, ensure_ascii=False), encoding="utf-8")

    novels = load_novels(paths)
    assert [s.id for s in novels[0].sources] == ["s1"]
    assert "没有 toc_url" in "；".join(store.LAST_LOAD_WARNINGS)


def test_site_proxies_values_are_normalized_to_text():
    """site_proxies 的值也得到读取层归一：数字 7890 会在抓取入口的 .strip() 上抛
    AttributeError，而那句在重试的 try 之外，一次坏记忆能让每轮都空跑。"""
    cfg = Config.from_dict({"site_proxies": {"book.example.com": 7890, "m.book.example.com": None}})
    assert cfg.site_proxies == {"book.example.com": "7890", "m.book.example.com": ""}


def test_broken_config_json_is_quarantined_and_defaults_rewritten(tmp_path, monkeypatch):
    """config.json 改坏：备份原文件，重写一份干净默认值，工具照常启动。"""
    monkeypatch.setenv("SHUNSHU_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True)
    cfg_path = tmp_path / "data" / "config.json"
    cfg_path.write_text("{oops,,,", encoding="utf-8")

    cfg = load_config()
    assert cfg.poll_minutes == Config().poll_minutes
    assert cfg_path.exists(), "备份走坏文件后必须重写干净默认值"
    assert list((tmp_path / "data").glob("config.json.*.bak")), "坏文件得留 .bak 现场"


# ---------- 名单合并与配置项 ----------

def test_builtin_ad_lists_always_merge_with_user_lists():
    """内置名单更新必须惠及老用户：用户配置与新内置名单合并，各自去重，用户的自定义项保留。"""
    cfg = Config(ad_exact_texts=["加入书签", "我的自定义广告文案"],
                 ad_patterns=["我的自定义广告正则"],
                 title_suffixes=["_我的自定义站名后缀$"])
    rules = Rules.build(cfg)

    assert normalize_text("我的自定义广告文案") in rules.exact
    assert normalize_text("加入书签") in rules.exact
    assert any(p.search("快来我的自定义广告正则") for p in rules.patterns)
    assert any(p.search("书名_我的自定义站名后缀") for p in rules.suffixes)

    # 默认配置编译结果必须恰好等于内置名单（union 逻辑不能往里塞重复/额外项）
    default_rules = Rules.build(Config())
    assert default_rules.exact == frozenset(
        normalize_text(t) for t in DEFAULT_AD_EXACT if normalize_text(t))
    assert len(default_rules.patterns) == len(DEFAULT_AD_PATTERNS)
    assert len(default_rules.suffixes) == len(DEFAULT_TITLE_SUFFIXES)


def test_toc_reset_new_ratio_is_configurable():
    """重编号阈值从配置读，默认 0.6（新标识占当前目录 60% 以上才判疑似重编号）。"""
    assert Config().toc_reset_new_ratio == 0.6
    assert Config(toc_reset_new_ratio=0.8).toc_reset_new_ratio == 0.8


def test_duplicate_source_ids_are_dropped_with_warning(paths: Paths):
    """整个来源块被复制进另一本书时 id 会撞车：轮询结果按来源 id 存字典，两个来源只剩
    一个有结果，另一本书从此再也不更新。加载阶段就当成坏数据挡掉并说明。"""
    from shunshu import store

    store.LAST_LOAD_WARNINGS.clear()
    shared = {"id": "s1", "toc_url": "https://x.example.com/1/", "site": "x"}
    paths.novels.write_text(json.dumps({"version": 2, "novels": [
        {"id": "n1", "title": "书一", "primary_id": "s1", "sources": [dict(shared)]},
        {"id": "n2", "title": "书二", "primary_id": "s1", "sources": [dict(shared)]},
        {"id": "n1", "title": "又一本 n1", "primary_id": "s9",
         "sources": [{"id": "s9", "toc_url": "https://y.example.com/2/"}]},
    ]}, ensure_ascii=False), encoding="utf-8")

    novels = load_novels(paths)
    assert [n.id for n in novels] == ["n1", "n2"], "整本重复的那条不该进来"
    assert novels[1].sources == [], "撞车的来源得挡掉"
    warnings = "；".join(store.LAST_LOAD_WARNINGS)
    assert "id 重复" in warnings and "整本重复" in warnings
