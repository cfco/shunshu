"""支持 `python -m shunshu` / `uv run shunshu`（控制台脚本入口在 cli.py）。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
