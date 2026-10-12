"""数据层韧性：日志按行轮转、坏 JSON 隔离备份、内置名单合并、新配置项。"""

from __future__ import annotations

import json

import fixtures
import pytest

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
