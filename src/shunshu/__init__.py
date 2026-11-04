"""顺手（shunshu）：小说目录页更新监控。"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _packaged_version() -> str:
    """Nuitka 产物里的版本号：没有 dist-info 就认产物根上那个 VERSION 文件。

    崩溃日志只写 v0.0.0 的话，现场认不出是哪一版（v0.16.0 在 Ubuntu 26.10 上崩那次就是
    这样）。CI 用 `--include-data-file` 把版本号打成数据文件，两个位置都要看：folder 模式
    在真 exe 旁边，onefile 模式在解包目录里（`__file__` 的上一级就是解包根）。
    """
    from . import packaging

    if not packaging.is_packaged():
        return ""
    for candidate in (Path(packaging.self_executable()).parent / "VERSION",
                      Path(__file__).resolve().parents[1] / "VERSION"):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


# 版本号只以 pyproject.toml 为单一来源：安装后从包元数据读，避免两处各写各的、
# 发版时漏改其中一处。产物里没有元数据，退而读打进来的 VERSION；源码目录直接跑（尚未
# 安装）时两处都没有，才是 "0.0.0"。
try:
    __version__ = version("shunshu")
except PackageNotFoundError:
    __version__ = _packaged_version() or "0.0.0"
