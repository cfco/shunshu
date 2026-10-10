"""跨来源章节对齐：不同站点、不同繁简写的同一章要归到同一个 match_key。

繁简转换只服务两件事：比对（canon_title）和展示统一（display_title）。
抓到的原始文本不改，跳过去的网页仍是源站原样。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from .models import strip_chapter_label

if TYPE_CHECKING:  # 只给类型注解用，避免运行时把 models 拖进来
    from .models import Chapter

_CANON_MAX = 24

try:
    from zhconv import convert as _zh_convert
except ModuleNotFoundError:  # 依赖没装也不能整个跑不起来：降级成不转繁简
    _zh_convert = None  # type: ignore[assignment]

# 括号里多是「求月票/加更/完」这类标注，各站加得不一样，比对时整段去掉
_PAREN_NOTE = re.compile(r"[（(][^（）()]{0,16}[）)]")
# 无编号章节的栏目名：各站写成「番外」「番外篇」「特別篇」，剥掉才比得出同一篇
_VOLUME_LABEL = re.compile(
    r"^\s*(?:特别番外篇|特别番外|特别篇|番外篇|番外|楔子|序章|序言|引子|前言|后记|尾声|终章|卷首语)"
    r"\s*[：:、.．\-—－_·]*"
)
# 只留汉字、数字、小写字母：空白/标点/全半角差异全部抹平
_NON_TITLE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


@lru_cache(maxsize=16384)
def to_simplified(text: str) -> str:
    """繁体转简体。转换失败就返回原文，绝不因为一个字把整轮检查打断。"""
    if _zh_convert is None or not text:
        return text
    try:
        return _zh_convert(text, "zh-cn")
    except Exception:
        return text


def display_title(title: str) -> str:
    """卡片/菜单上显示用的标题：统一成简体，其余原样。"""
    return to_simplified(title or "")


def canon_title(title: str) -> str:
    """「第120章 風雪夜歸人（求月票）」→「风雪夜归人」。

    整条只是序号（「第120章」）时返回空串，让 match_key 用编号认身份；
    整条只是栏目名（「番外」）时保留栏目名，否则所有番外会并成一章。
    """
    simplified = to_simplified(title or "")
    body = strip_chapter_label(simplified)
    if not body:
        return ""
    stripped = _PAREN_NOTE.sub("", _VOLUME_LABEL.sub("", body, count=1))
    core = _NON_TITLE.sub("", stripped.lower())
    if not core:
        core = _NON_TITLE.sub("", body.lower())
    return core[:_CANON_MAX]


def match_key(number: int | None, title: str) -> str:
    """小说级的章节身份：编号 + 正题。编号缺失时只能靠正题。"""
    core = canon_title(title)
    if not core:
        core = f"#{number}" if number is not None else "?"
    return f"{number:06d}|{core}" if number is not None else f"?|{core}"


def key_of(chapter: Chapter) -> str:
    return match_key(chapter.number, chapter.title)
