"""发布页正文的取文本逻辑（scripts/release_notes.py）。

这里守的是「README 的日志格式与 CI 取正文的那段正则不能各改各的」：格式一改，
脚本就匹配不到条目，发布页正文会静默退化成一句占位提示——产物照样发出去，
没人会去点开发布页第二眼。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.release_notes import entries, main, render

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"


def test_current_version_has_a_changelog_entry():
    """pyproject 里那个版本号，必须在 README 变更记录里有条目（发版前抬版本时别漏写）。"""
    version = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"),
                        re.M).group(1)
    found = {m["version"].strip() for m in entries(README.read_text(encoding="utf-8"))}
    assert version in found, f"v{version} 在 README「变更记录」里没有对应条目，发布页会写成占位提示"


def test_render_turns_the_one_line_entry_into_bullets():
    """README 里一条日志是一整行（①②③ 挤在同一段），发布页要按带圈数字拆成条目。"""
    body = "开头那句摘要。① **改了甲**：说明甲。② **改了乙**：说明乙。"
    text = render("1.2.3", "2026-10-28", body)

    assert text.splitlines()[0] == "## v1.2.3（2026-10-28）"
    assert "开头那句摘要。" in text
    assert "- **①** **改了甲**：说明甲。" in text
    assert "- **②** **改了乙**：说明乙。" in text
    assert text.endswith("\n")


def test_latest_entry_from_the_real_readme_parses():
    """真 README 的第一条（最新那版）要能解析出日期与非空正文。"""
    latest = entries(README.read_text(encoding="utf-8"))[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", latest["date"])
    assert len(latest["body"]) > 100


def test_missing_entry_writes_a_placeholder_and_does_not_fail(tmp_path, capsys):
    """找不到条目不能让发版步骤红：产物已经编好了，改成占位提示 + stderr 警告。"""
    out = tmp_path / "notes.md"
    assert main(["--version", "9.9.9", "--readme", str(README), "--out", str(out)]) == 0

    text = out.read_text(encoding="utf-8")
    assert "v9.9.9" in text and "变更记录" in text
    assert "警告" in capsys.readouterr().err
