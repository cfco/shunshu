"""跨来源章节对齐：繁简、编号写法、括号标注都不该把同一章拆成两章。"""

from __future__ import annotations

from shunshu.align import canon_title, display_title, match_key, to_simplified
from shunshu.models import parse_chapter_number


def key_for(title: str) -> str:
    return match_key(parse_chapter_number(title), title)


def test_traditional_and_simplified_share_one_key():
    assert key_for("第120章 风雪夜归人") == key_for("第120章 風雪夜歸人")


def test_number_styles_collapse_to_the_same_number():
    assert parse_chapter_number("第廿三章 旧事") == 23
    assert parse_chapter_number("第壹佰貳拾叁章 旧事") == 123
    assert parse_chapter_number("第贰拾叁章 旧事") == 23
    assert parse_chapter_number("第一万零八百八十八章 旧事") == 10888


def test_label_prefix_variants_do_not_change_the_key():
    same = [
        "第120章 风雪夜归人",
        "第一百二十章 风雪夜归人",
        "第120章：风雪夜归人",
        "正文 第120章 风雪夜归人",
        "120 风雪夜归人",
    ]
    assert len({key_for(t) for t in same}) == 1


def test_parenthetical_notes_are_dropped_for_matching():
    assert key_for("第120章 风雪夜归人（求月票）") == key_for("第120章 风雪夜归人")
    assert key_for("第120章 风雪夜归人(加更)") == key_for("第120章 风雪夜归人")


def test_chapter_without_any_title_falls_back_to_number():
    assert key_for("第120章") == "000120|#120"
    assert key_for("第121章") != key_for("第120章")


def test_unnumbered_chapters_match_by_title_only():
    assert key_for("番外 若小妖") == key_for("番外篇·若小妖")
    assert key_for("番外 若小妖") != key_for("番外 东海潮")


def test_bare_volume_labels_keep_their_own_key():
    """整条标题就是「番外」时不能剥成空串，否则所有番外会并成一章。"""
    assert key_for("番外") == "?|番外"
    assert key_for("楔子") == "?|楔子"
    assert key_for("番外") != key_for("楔子")


def test_different_titles_stay_different():
    assert key_for("第120章 风雪夜归人") != key_for("第120章 山有木兮")


def test_display_title_stays_simplified_but_keeps_the_label():
    assert display_title("第120章 風雪夜歸人") == "第120章 风雪夜归人"


def test_canon_title_is_short_enough_to_be_stable():
    long = "第1章 " + "风雪夜归人" * 12
    assert len(canon_title(long)) <= 24


def test_missing_zhconv_degrades_to_original_text(monkeypatch):
    import shunshu.align as align

    monkeypatch.setattr(align, "_zh_convert", None)
    to_simplified.cache_clear()
    assert to_simplified("風雪夜歸人") == "風雪夜歸人"
    monkeypatch.setattr(align, "_zh_convert", lambda text, target: "风雪夜归人")
    to_simplified.cache_clear()
    assert to_simplified("風雪夜歸人") == "风雪夜归人"
    to_simplified.cache_clear()
