"""把 README「变更记录」里某版本那一条，排成 GitHub Release 的正文。

README 里每条更新日志是一整行（①②③ 的说明全挤在同一段），原样贴到发布页就是一坨
看不见边界的长文；这里按带圈数字拆成一条条项目符号。发布页要的是**实际改动内容**，
所以 workflow 里关掉 GitHub 自动生成 notes（那种是 PR 清单加一串链接）。

两个 job 都调这一个脚本：Windows 那次建 Release、Ubuntu 那次往同一个 Release 追加资产，
正文必须一模一样，不能各写一份。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECTION = "## 变更记录"
ENTRY = re.compile(r"^- \*\*(?P<version>[0-9][^*（]*)（(?P<date>[^）]*)）\*\*：(?P<body>.*)$")
MARKER = re.compile(r"([①-⑳])")


def entries(readme_text: str) -> list[re.Match]:
    """变更记录那一节里的版本条目（按 README 原序，最新在上）。"""
    lines = readme_text.splitlines()
    try:
        start = lines.index(SECTION)
    except ValueError:
        return []
    found = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        match = ENTRY.match(line.strip())
        if match:
            found.append(match)
    return found


def render(version: str, date: str, body: str) -> str:
    """一条更新日志 → 发布页正文：开头是那句摘要，①②③ 各占一行。"""
    head, *parts = MARKER.split(body.strip())
    text = [f"## v{version}（{date}）", "", head.strip(), ""]
    for marker, content in zip(parts[0::2], parts[1::2]):
        text += [f"- **{marker}** {content.strip()}", ""]
    return "\n".join(text).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="版本号，带不带 v 都行")
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--out", required=True, help="写出正文文件（GitHub 的 body_path）")
    args = parser.parse_args(argv)

    version = args.version.lstrip("v")
    readme = Path(args.readme).read_text(encoding="utf-8")
    for match in entries(readme):
        if match["version"].strip() == version:
            body = render(version, match["date"].strip(), match["body"])
            Path(args.out).write_text(body, encoding="utf-8")
            print(f"发布正文取 README 变更记录 v{version}（{len(body)} 字符）")
            return 0

    # 找不到不拦发版：产物已经编好了，别因为漏写一条日志就把整次发布判失败
    fallback = (f"## v{version}\n\n"
                f"README 的「变更记录」里没有 v{version} 这一条，补一下；"
                f"完整改动请看仓库根 README.md 与提交历史。\n")
    Path(args.out).write_text(fallback, encoding="utf-8")
    print(f"警告：README 变更记录里没有 v{version}，正文写成占位提示", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
