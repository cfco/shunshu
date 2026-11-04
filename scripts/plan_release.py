"""发版号规划：每次 push 都要发一个**没发过**的包，不覆盖已经发出去的 Release。

规则很简单：pyproject 里那个版本号还没打过 tag 就直接用它（正常发版就是「改版本号 + push」）；
tag 已经存在——要么这版已经发过，要么你忘抬版本号又推了一次——就 patch 一路 +1 找第一个空闲的
号。于是每次推送都是一份新包，历史下载链接不会今天还是明天就变成别的内容。

两个编译 job 用同一个脚本：`plan` job 先算并把版本号写进 pyproject/uv.lock，两个 build job
再用 `--set-version` 把自己检出里的文件改成同一个号——wheel 的文件名与元数据、deb 的 Version、
产物里的 VERSION 都从 pyproject 取，所以三处必须一致，否则 Release 叫 v0.17.1 而包里读出来是
0.17.0。

用法：
    python3 scripts/plan_release.py --tags .tmp/tags.txt --apply        # 算下一个空闲号并改写文件
    python3 scripts/plan_release.py --set-version 0.17.1 --apply        # 照 plan 的结论改写文件
stdout 只输出那一个版本号，其余话都走 stderr。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# git ls-remote --tags 的输出里，一个 tag 会出现普通行与 peeled 行（v0.17.0^{}），都算同一个号
TAG_LINE = re.compile(r"refs/tags/v(\d+\.\d+\.\d+)\b")
# pyproject 里第一个 ^version = 就是 [project] 的 version（它排在 name 下面、别的段之前）
PYPROJECT_VERSION = re.compile(r'(?m)^version = "[^"]*"')
LOCK_NAME = re.compile(r'^name = "(?P<name>[^"]+)"$')


def read_current_version(pyproject_text: str) -> str:
    match = PYPROJECT_VERSION.search(pyproject_text)
    if not match:
        raise ValueError("pyproject.toml 里找不到 version = \"...\" 这一行")
    return match.group(0).split('"')[1]


def taken_versions(tags_text: str) -> set[str]:
    return {m.group(1) for line in tags_text.splitlines() if (m := TAG_LINE.search(line))}


def next_free(current: str, used: set[str]) -> str:
    parts = current.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"版本号必须是 X.Y.Z 三段数字，现在是 {current!r}")
    major, minor, patch = (int(p) for p in parts)
    while f"{major}.{minor}.{patch}" in used:
        patch += 1
    return f"{major}.{minor}.{patch}"


def apply_pyproject_version(pyproject_text: str, version: str) -> str:
    return PYPROJECT_VERSION.sub(f'version = "{version}"', pyproject_text, count=1)


def apply_lock_version(lock_text: str, version: str, package: str = "shunshu") -> tuple[str, bool]:
    """只改 uv.lock 里 `[[package]] name = "<package>"` 那一块的 version。

    锁文件每个依赖都有一行 version，全局替换会把不相干的包一起改掉。
    """
    lines = lock_text.splitlines(keepends=True)
    out: list[str] = []
    in_block = False
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[[package]]":
            in_block = False
        elif (m := LOCK_NAME.match(stripped)):
            in_block = m.group("name") == package
        elif in_block and stripped.startswith("version = "):
            line = f'version = "{version}"\n'
            in_block = False
            done = True
        out.append(line)
    return "".join(out), done


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tags", help="git ls-remote --tags origin 的输出文件；不给就当成没发过任何版")
    parser.add_argument("--pyproject", default="pyproject.toml")
    parser.add_argument("--lock", default="uv.lock")
    parser.add_argument("--set-version", help="照 plan job 的结论直接用这个号（不再找空闲号）")
    parser.add_argument("--apply", action="store_true", help="把选中的号写回 pyproject 与 lock")
    args = parser.parse_args(argv)

    pyproject = Path(args.pyproject).read_text(encoding="utf-8")
    try:
        current = read_current_version(pyproject)
        if args.set_version:
            version = args.set_version
        else:
            tags_text = Path(args.tags).read_text(encoding="utf-8") if args.tags else ""
            version = next_free(current, taken_versions(tags_text))
            if version != current:
                # 只有真去找空闲号时才说「已经发过」；--set-version 是照 plan 的结论改写文件，
                # 那个结论本来就允许与 pyproject 不同
                print(f"v{current} 已经发过了（或这一版没抬号），本次发成 v{version}", file=sys.stderr)
    except ValueError as exc:
        print(f"发版号规划失败：{exc}", file=sys.stderr)
        return 2

    if args.apply:
        Path(args.pyproject).write_text(apply_pyproject_version(pyproject, version), encoding="utf-8")
        lock_path = Path(args.lock)
        if lock_path.exists():
            text = lock_path.read_text(encoding="utf-8")
            new_text, done = apply_lock_version(text, version)
            if not done:
                print(f"{args.lock} 里没找到 shunshu 那个包块，锁文件没改", file=sys.stderr)
                return 2
            lock_path.write_text(new_text, encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
