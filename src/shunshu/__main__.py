"""支持 `python -m shunshu` / `uv run shunshu`（控制台脚本入口在 cli.py）。"""

try:
    from .cli import main
except ImportError:  # Nuitka 把本文件当顶层脚本编译，没有父包上下文，只能用绝对导入
    from shunshu.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
