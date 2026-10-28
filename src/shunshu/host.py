"""运行平台判定：Windows 与 Linux 的差异统一从这里分流。

各模块别再自己猜 sys.platform——判错一处的现场是「Ubuntu 上启动就崩」或
「Windows 上卡片属性丢了」，两边都难查。平台分支必须写在函数体内读这个常量，
写在模块导入期就固化了，测试没法在一台机器上跑另一套分支。
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"
