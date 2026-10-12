"""data/ 目录读写：novels.json 原子写，history 为 append-only jsonl。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import data_dir
from .models import Novel, Source

_LOCK = threading.RLock()
SCHEMA_VERSION = 2

# 追加型日志（shunshu.log / filtered.log / alerts.log / history/*.jsonl）超过这个
# 字节数就按行边界裁掉开头，只留尾部一段，避免长跑几年把磁盘吃掉
_ROTATE_BYTES = 2 * 1024 * 1024
_ROTATE_KEEP_RATIO = 0.75

# 最近一次加载（config.json / novels.json）遇到的降级说明：AppState.reload 取走弹卡
LAST_LOAD_WARNINGS: list[str] = []

# read_jsonl 只从文件尾部读这么多个字节：够取最后几十行即可，不整读 2MB 轮转文件
_READ_TAIL_BYTES = 256 * 1024


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def source_id_for(toc_url: str) -> str:
    return hashlib.sha1(toc_url.strip().encode("utf-8")).hexdigest()[:12]


class Paths:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir()

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def novels(self) -> Path:
        return self.root / "novels.json"

    @property
    def history(self) -> Path:
        return self.root / "history"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates"

    @property
    def filtered_log(self) -> Path:
        return self.root / "filtered.log"

    @property
    def alerts_log(self) -> Path:
        return self.root / "alerts.log"

    @property
    def read_log(self) -> Path:
        """卡片进程把「点开过的章节」追加到这里，主程序下一轮吸收进书架。"""
        return self.root / "read.jsonl"

    @property
    def runtime_log(self) -> Path:
        return self.root / "shunshu.log"

    def source_history(self, novel_id: str, source_id: str) -> Path:
        """按书分目录、按来源分文件：多来源的更新轨迹要能分开看。"""
        return self.history / novel_id / f"{source_id}.jsonl"

    def source_candidates(self, source_id: str) -> Path:
        return self.candidates / f"{source_id}.txt"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)
        self.candidates.mkdir(parents=True, exist_ok=True)


def quarantine_broken_json(path: Path, reason: str) -> Path | None:
    """把读不动的 JSON 改名隔离成 .bak，腾出位置让上层重写一份干净默认值。

    文件名带时间戳：连续两次改坏也不会覆盖掉前一份现场。返回备份路径，失败返回 None。
    """
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    try:
        path.replace(backup)
    except OSError:
        return None
    LAST_LOAD_WARNINGS.append(
        f"{path.name} 已损坏（{reason}），原文件备份为 {backup.name}，本次按空数据启动")
    try:
        append_text(path.parent / "shunshu.log", f"{now_stamp()} 配置降级：{path} {reason}")
    except OSError:
        pass
    return backup


def load_novels(paths: Paths) -> list[Novel]:
    """读书架；novels.json 不存在返回空书架，手改坏了就隔离备份并按空书架启动。"""
    if not paths.novels.exists():
        return []
    with _LOCK:
        try:
            raw = json.loads(paths.novels.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            quarantine_broken_json(paths.novels, f"{type(exc).__name__}: {exc}")
            return []
    if not isinstance(raw, (dict, list)):
        quarantine_broken_json(paths.novels, f"顶层类型是 {type(raw).__name__}，不是对象/数组")
        return []
    items = raw.get("novels", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        LAST_LOAD_WARNINGS.append(
            f"novels.json 里没有 novels 数组（是 {type(items).__name__}），本次按空书架启动")
        return []
    novels: list[Novel] = []
    seen_novel_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    for item in items:
        # 单本写坏不连累整架：跳过那一条并留警告，其余照常加载
        if not isinstance(item, dict):
            LAST_LOAD_WARNINGS.append(f"novels.json 里有一条不是对象（{type(item).__name__}），已跳过")
            continue
        try:
            novel = Novel.from_dict(item)
        except (TypeError, ValueError, AttributeError) as exc:
            LAST_LOAD_WARNINGS.append(
                f"novels.json 里有一条读不出来（{type(exc).__name__}: {exc}），已跳过")
            continue
        if not novel.id:
            LAST_LOAD_WARNINGS.append("novels.json 里有一条没有 id，已跳过")
            continue
        if novel.id in seen_novel_ids:
            LAST_LOAD_WARNINGS.append(f"《{novel.title or novel.id}》整本重复（id 一样），已跳过")
            continue
        seen_novel_ids.add(novel.id)
        # 小说 id 与来源 id 各查各的：add_novel 让新书的 novel.id 等于首源 id，
        # 混在一个集合里会让每本书的第一个来源都「自己跟自己重复」
        _repair_sources(novel, seen_source_ids)
        if not novel.sources and item.get("sources"):
            LAST_LOAD_WARNINGS.append(
                f"《{novel.title or novel.id}》的 sources 里没有一条能用（写坏的已跳过）")
        novels.append(novel)
    return novels


def _repair_sources(novel: Novel, seen_source_ids: set[str]) -> None:
    """补齐来源 id、丢掉抓不了的来源，并把重复的 id 挡在门外。

    id 本就是目录页 URL 的 sha1 前 12 位，缺了就按 toc_url 补算。整个来源块被复制进
    另一本书时 id 会撞车：轮询结果按来源 id 存字典，撞车的两个来源只剩一个有结果，
    另一本书从此永远不更新，所以这里就当坏数据挡掉。
    """
    kept: list[Source] = []
    for source in novel.sources:
        if not source.toc_url:
            LAST_LOAD_WARNINGS.append(
                f"《{novel.title or novel.id}》有个来源没有 toc_url，已跳过该来源")
            continue
        if not source.id:
            source.id = source_id_for(source.toc_url)
        if source.id in seen_source_ids:
            LAST_LOAD_WARNINGS.append(
                f"《{novel.title or novel.id}》的来源 {source.site or source.id} id 重复，已跳过该来源")
            continue
        seen_source_ids.add(source.id)
        kept.append(source)
    novel.sources = kept


def save_novels(paths: Paths, novels: list[Novel]) -> None:
    payload = {"version": SCHEMA_VERSION, "novels": [n.to_dict() for n in novels]}
    write_json_atomic(paths.novels, payload)


def find_novel(novels: list[Novel], ident: str) -> Novel | None:
    """novel id、任一来源 id 或目录页 URL 都能定位到同一本小说。"""
    for novel in novels:
        if novel.matches(ident):
            return novel
    return None


def write_json_atomic(path: Path, payload: dict) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _rotate_if_needed(path: Path) -> None:
    """追加前检查体积：超阈值就按行边界只留尾部，防止日志无限增长。

    调用方必须已持有 _LOCK。裁剪失败（占用/权限）静默跳过，下一轮再试，
    绝不能因为轮转日志这件小事把正常抓取流程弄挂。
    """
    try:
        if not path.exists() or path.stat().st_size <= _ROTATE_BYTES:
            return
        keep_bytes = int(_ROTATE_BYTES * _ROTATE_KEEP_RATIO)
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - keep_bytes))
            fh.readline()  # 丢掉落点所在的半行，保证留下的每行都完整
            tail = fh.read()
        tmp = path.with_name(path.name + ".trim")
        tmp.write_bytes(tail)
        os.replace(tmp, path)
    except OSError:
        pass


def append_jsonl(path: Path, record: dict) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _tail_lines(path: Path) -> list[str]:
    """只读文件尾部一段再按行切：history jsonl 单文件可轮转到 2MB，
    而 read_jsonl 每次只要最后几十行，整读全文再切尾太浪费（书多时反复读几 MB）。

    尾段不够取时最多少返回几行（不会多给），256KB 远大于实际需要的几十行。
    解码失败的半行按替换符吞掉，交给下面的 json 解析去跳过。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= _READ_TAIL_BYTES:
        try:
            return path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            return []
    try:
        with path.open("rb") as fh:
            fh.seek(size - _READ_TAIL_BYTES)
            chunk = fh.read()
    except OSError:
        return []
    nl = chunk.find(b"\n")
    if nl < 0:
        return []  # 一整段没有换行，说明不是有效的行式日志
    return chunk[nl + 1:].decode("utf-8", "replace").splitlines()


def read_jsonl(path: Path, limit: int = 50) -> list[dict]:
    if not path.exists():
        return []
    with _LOCK:
        lines = _tail_lines(path)
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


def drain_jsonl(path: Path) -> list[dict]:
    """读空一个 jsonl：卡片进程写的已读记录，主程序吸收后就不能再重复算。"""
    if not path.exists():
        return []
    with _LOCK:
        # 先改名再取走：读和清之间卡片可能刚好追加一条，改名是原子的，不会把那条丢掉
        tmp = path.with_name(f"{path.name}.{os.getpid()}.draining")
        try:
            path.rename(tmp)
        except OSError:
            return []
        try:
            with tmp.open("rb") as fh:
                data = fh.read()
                # 读完到删除之间卡片又写了一条的：同一个句柄接着读能拿到，
                # 原样搬回正主文件交给下一轮吸收，别连着 .draining 一起 unlink 掉
                late = fh.read()
        except OSError:
            return []
        if late:
            try:
                with path.open("ab") as fh:
                    fh.write(late)
            except OSError:
                pass
        tmp.unlink(missing_ok=True)
        text = data.decode("utf-8-sig", "replace")
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_text(path: Path, text: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def log_filtered(paths: Paths, source: Source, dropped: list[tuple[str, str, str]]) -> None:
    """被过滤掉的链接按来源落盘，误杀时能从里面找到 key 填回 keep_keys。"""
    if not dropped:
        return
    stamp = now_stamp()
    lines = [f"{stamp} [{source.id}] {reason} | {text} | {url}" for reason, text, url in dropped[:200]]
    append_text(paths.filtered_log, "\n".join(lines))


def write_candidates(paths: Paths, novel: Novel, source: Source, candidates: list,
                     note: str = "") -> None:
    blocks: list[str] = [
        f"# 《{novel.title or novel.id}》来源 {source.site or source.id} 目录识别失败候选容器",
        f"# 生成时间: {now_stamp()}",
        f"# 目录页: {source.toc_url}",
    ]
    if note:
        blocks.append(f"# 说明: {note}")
    blocks.append("# 选一个最像的，把它的 CSS 填进 data/novels.json 里该书 sources 中本源的 \"selector\"，然后托盘「重新加载配置」。")
    for i, cand in enumerate(candidates or [], 1):
        blocks.append(f"\n## 候选 {i}\n{cand.describe()}")
    blocks.append("\n# 被过滤掉的链接（前 40 条）：确认误杀后把对应 URL 填进本源 keep_keys")
    write_text_atomic(paths.source_candidates(source.id), "\n".join(blocks) + "\n")


def sleep_seconds(seconds: float) -> None:
    time.sleep(max(0.0, seconds))
