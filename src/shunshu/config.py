"""配置：默认名单 + data/config.json 读写。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import CN_LABEL_CHARS, CN_NUM_CHARS
from .packaging import is_packaged, self_executable

APP_NAME = "shunshu"
# 托盘菜单里扫描间隔的固定选项：5/10/30/60 分钟，用户只能四选一
POLL_MINUTES = (5, 10, 30, 60)

# 整条文本命中即丢弃（归一化后比较）：目录页上的功能性链接与站名水印
DEFAULT_AD_EXACT = [
    "加入书签", "加入收藏", "收藏本书", "推荐本书", "点击收藏", "报错", "举报", "缺章反馈",
    "返回目录", "返回书目", "返回书页", "回到目录", "目录", "书页", "首页", "上一页", "下一页",
    "下页", "上页", "上一章", "下一章", "更多", "全部", "查看全部", "查看全部章节", "展开", "收起",
    "开始阅读", "立即阅读", "在线阅读", "正文", "完本感言", "作品相关", "内容简介", "作者的话",
    "相关作品", "笔趣阁", "最新章节", "最新章节列表", "正在加载中", "加载中", "此章节已屏蔽",
    "章节报错", "为本作品加油", "下载本书", "txt下载", "无删版", "全文阅读",
]

# 非章节样式的文本里出现这些即判为广告；章节样式标题不走这里，避免「第3章（求月票）」被误杀
DEFAULT_AD_PATTERNS = [
    r"txt.{0,6}下载", r"下载.{0,6}txt", r"全文.{0,4}下载", r"app.{0,8}(下载|安装|客户端)",
    r"客户端.{0,6}(下载|安装)", r"手机.{0,4}(在线阅读|客户端)", r"记住.{0,8}(本站|本网|地址)",
    r"请收藏", r"(无弹窗|无广告).{0,8}(阅读|下载)", r"新域名", r"(威|微|薇).{0,4}(信|号|聊)",
    r"(qq|电报|telegram).{0,8}(群|频道)", r"(赌|彩|真人)(娱乐|注册)", r"(ag|ky|ng|ob|乐).{0,6}(娱乐|棋牌|注册)",
    r"(百度|uc).{0,6}浏览器", r"广告", r"(小说|读书).{0,4}(app|软件)", r"免费下载.{0,6}(软件|app)",
]

# 标题尾部/中部的站名与推广后缀，清洗后不参与章节号解析
DEFAULT_TITLE_SUFFIXES = [
    r"[_\-—|·\s]*(新?笔趣阁|全文免费阅读|无弹窗|手机在线阅读|最快更新|顶点小说|读小说|小说网|请收藏\S{0,12})$",
    r"[（(](?:无弹窗|求订阅|求月票|月票|首发起订|新增读者群)[）)]",
]

CHAPTER_HINT = re.compile(
    rf"第\s*[0-9{re.escape(CN_NUM_CHARS)}]+\s*[{CN_LABEL_CHARS}]"
)
# 广告位常见容器命名，用边界匹配避免误伤 read/adm 之类
AD_NODE_HINT = re.compile(
    r"(?:^|[\s,_-])(?:ad|ads|advert|banner|tongji|count|push|float|guanggao|gg|hidden|mfsy)(?:$|[\s,_-])",
    re.I,
)
_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x00A0, 0x3000], None
)


def normalize_text(text: str) -> str:
    """名单匹配用：去空白与零宽字符并转小写。"""
    return re.sub(r"\s+", "", (text or "").translate(_ZERO_WIDTH)).lower()


def _base_dir() -> Path:
    """程序基准目录：data/ 就建在它旁边。

    打包态取真实 exe（sys.argv[0]）所在目录。Nuitka onefile 运行时 __file__ 位于
    按版本变化的解包临时目录里，用它定位会导致每次升级后书架与 site_proxies 记忆
    全部"丢失"。源码态向上找 pyproject.toml 所在的项目根。
    """
    if is_packaged():
        return Path(self_executable()).parent
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("SHUNSHU_DATA_DIR")
    return Path(override) if override else _base_dir() / "data"


@dataclass(slots=True)
class Config:
    poll_minutes: int = 30
    concurrency: int = 6
    same_host_delay_seconds: float = 2.0
    request_timeout_seconds: float = 15.0
    request_retries: int = 2
    retry_backoff_seconds: float = 3.0
    max_response_bytes: int = 3_000_000
    min_container_links: int = 8
    min_chapterish_links: int = 3
    max_pages_followed: int = 1
    history_display_cap: int = 10
    notified_keys_keep: int = 500
    max_report_new_chapters: int = 30
    toc_reset_overlap_ratio: float = 0.35
    # 一次冒出的新标识占当前目录比例超过此值就判「疑似重编号」，只记录不提醒
    toc_reset_new_ratio: float = 0.6
    # 目录正/倒序判断：编号升序占比超过此阈值就认作正序，低于 (1 - 阈值) 认作倒序
    order_trend_threshold: float = 0.6
    failure_backoff_threshold: int = 5
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    ad_exact_texts: list[str] = field(default_factory=lambda: list(DEFAULT_AD_EXACT))
    ad_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_AD_PATTERNS))
    title_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_SUFFIXES))
    host_whitelist: list[str] = field(default_factory=list)
    # 添加书源时自动探测的站点代理记忆：主机名（去 www）→ 代理 URL；
    # 空串表示探测过、直连可达。同站点再添来源直接套用，不再探测。可手改/删键后重新探测。
    site_proxies: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> Config:
        cfg = cls()
        for key, value in (data or {}).items():
            if key not in cls.__dataclass_fields__ or value is None:
                continue
            # 手改 config.json 把字典字段写成别的类型时别让整个工具起不来
            if key == "site_proxies" and not isinstance(value, dict):
                continue
            setattr(cfg, key, value)
        return cfg

    def compiled(self) -> Rules:
        return Rules.build(self)


class Rules:
    """预编译名单与正则，避免每轮检查重复编译。"""

    def __init__(self, cfg: Config, exact: frozenset[str], patterns: list[re.Pattern],
                 suffixes: list[re.Pattern], whitelist: frozenset[str]):
        self.cfg = cfg
        self.exact = exact
        self.patterns = patterns
        self.suffixes = suffixes
        self.whitelist = whitelist

    @classmethod
    def build(cls, cfg: Config) -> Rules:
        # 内置名单始终并进用户配置：老用户 config.json 里固化的旧名单也能收到版本更新
        # 带来的新默认项；用户自己加的条目原样保留，按首次出现顺序去重
        return cls(
            cfg,
            frozenset(filter(None, (
                normalize_text(t) for t in _union_lists(DEFAULT_AD_EXACT, cfg.ad_exact_texts)))),
            _compile(_union_lists(DEFAULT_AD_PATTERNS, cfg.ad_patterns), re.I),
            _compile(_union_lists(DEFAULT_TITLE_SUFFIXES, cfg.title_suffixes), 0),
            frozenset((h or "").strip().lower() for h in cfg.host_whitelist),
        )


def _union_lists(builtin: list[str], custom: list[str]) -> list[str]:
    """内置项在前、用户项在后，按原文去重保序：内置名单更新所有用户都能立刻用上。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in [*builtin, *(custom or [])]:
        if item is None or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _compile(patterns: list[str], flags: int) -> list[re.Pattern]:
    out: list[re.Pattern] = []
    for pattern in patterns:
        try:
            out.append(re.compile(pattern, flags))
        except re.error:
            continue  # 用户手改名单写错正则时不至于让整个工具起不来
    return out


def config_path() -> Path:
    return data_dir() / "config.json"


def load_config(create_if_missing: bool = True) -> Config:
    """读 config.json；文件不存在就生成默认配置，手改坏了就隔离备份并退回默认值。

    损坏时不让工具起不来：原文件改名成带时间戳的 .bak，重写一份干净默认配置，
    降级说明经 store.LAST_LOAD_WARNINGS 交给托盘弹卡。
    """
    path = config_path()
    if not path.exists():
        cfg = Config()
        if create_if_missing:
            save_config(cfg)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        from .store import quarantine_broken_json

        quarantine_broken_json(path, f"{type(exc).__name__}: {exc}")
        cfg = Config()
        if create_if_missing:
            save_config(cfg)  # 立刻补一份干净默认值，之后手改只有这一份文件
        return cfg
    if not isinstance(data, dict):
        from .store import quarantine_broken_json

        quarantine_broken_json(path, f"顶层类型是 {type(data).__name__}，不是 JSON 对象")
        cfg = Config()
        if create_if_missing:
            save_config(cfg)
        return cfg
    return Config.from_dict(data)


def save_config(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
