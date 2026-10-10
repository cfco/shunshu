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
    def legacy_books(self) -> Path:
        """v1 的书架文件：只在首次读到时转换成 novels.json。"""
        return self.root / "books.json"

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


def load_novels(paths: Paths) -> list[Novel]:
    if paths.novels.exists():
        with _LOCK:
            raw = json.loads(paths.novels.read_text(encoding="utf-8-sig"))
        items = raw.get("novels", raw) if isinstance(raw, dict) else raw
        return [Novel.from_dict(item) for item in items]
    return migrate_v1(paths)


def migrate_v1(paths: Paths) -> list[Novel]:
    """v1 的一本 Book 就是一个来源：包成「一本小说挂一个来源」沿用原 id。

    v1 的 read_keys 记的是归一化 URL，v2 已读改用跨源 match_key，旧标记比不中，
    留着也不会误判成已读，会被 notified_keys_keep 上限自然挤掉。
    """
    if not paths.legacy_books.exists():
        return []
    with _LOCK:
        raw = json.loads(paths.legacy_books.read_text(encoding="utf-8-sig"))
    items = raw.get("books", raw) if isinstance(raw, dict) else raw
    novels: list[Novel] = []
    for item in items:
        source = Source.from_dict(item)
        novels.append(Novel(id=source.id, title=source.title, sources=[source],
                            primary_id=source.id, read_keys=list(item.get("read_keys") or []),
                            created_at=source.created_at))
    return novels


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


def append_jsonl(path: Path, record: dict) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path, limit: int = 50) -> list[dict]:
    if not path.exists():
        return []
    with _LOCK:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
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
        text = tmp.read_text(encoding="utf-8-sig")
        tmp.unlink(missing_ok=True)
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
    from .toc_detect import Candidate

    blocks: list[str] = [
        f"# 《{novel.title or novel.id}》来源 {source.site or source.id} 目录识别失败候选容器",
        f"# 生成时间: {now_stamp()}",
        f"# 目录页: {source.toc_url}",
    ]
    if note:
        blocks.append(f"# 说明: {note}")
    blocks.append("# 选一个最像的，把它的 CSS 填进 data/novels.json 里该书 sources 中本源的 \"selector\"，然后托盘「重新加载配置」。")
    for i, cand in enumerate(candidates or [], 1):
        assert isinstance(cand, Candidate)
        blocks.append(f"\n## 候选 {i}\n{cand.describe()}")
    blocks.append("\n# 被过滤掉的链接（前 40 条）：确认误杀后把对应 URL 填进本源 keep_keys")
    write_text_atomic(paths.source_candidates(source.id), "\n".join(blocks) + "\n")


def sleep_seconds(seconds: float) -> None:
    time.sleep(max(0.0, seconds))
