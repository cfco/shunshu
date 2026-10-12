"""顺手（shunshu）：小说目录页更新监控。"""

from importlib.metadata import PackageNotFoundError, version

# 版本号只以 pyproject.toml 为单一来源：安装后从包元数据读，避免两处各写各的、
# 发版时漏改其中一处。源码目录直接跑（尚未安装）时退回到 "0.0.0"。
try:
    __version__ = version("shunshu")
except PackageNotFoundError:  # pragma: no cover - 仅未安装的开发环境会走到
    __version__ = "0.0.0"
