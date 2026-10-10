"""应用状态：书架增删改 + 跑一轮检查 + 落盘，托盘和命令行共用。"""

from __future__ import annotations

import threading
from pathlib import Path

from .align import match_key
from .config import Config, Rules, load_config, save_config
from .http import Fetcher, assert_http_url
from .models import (
    STATUS_BASELINE,
    STATUS_NO_CHANGE,
    STATUS_UPDATED,
    Chapter,
    Novel,
    NovelAlert,
    Outcome,
    Source,
    host_label,
    site_key,
    status_label,
)
from .monitor import check_all, check_source, collect_alerts
from .notify import Notifier
from .store import (
    Paths,
    append_text,
    drain_jsonl,
    find_novel,
    load_novels,
    now_stamp,
    read_jsonl,
    save_novels,
    sleep_seconds,
    source_id_for,
)


class DuplicateSource(Exception):
    """同一目录页已在书架里（不管挂在哪个来源下）。"""


class AppState:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or Paths()
        self.paths.ensure()
        self._lock = threading.RLock()
        self.last_alerts: list[NovelAlert] = []
        self.reload()

    # ---------- 配置与书架 ----------

    @property
    def cfg(self) -> Config:
        return self._cfg

    @property
    def rules(self) -> Rules:
        return self._rules

    @property
    def novels(self) -> list[Novel]:
        with self._lock:
            return list(self._novels)

    @property
    def sources(self) -> list[Source]:
        with self._lock:
            return [s for n in self._novels for s in n.sources]

    def novel(self, ident: str) -> Novel | None:
        with self._lock:
            return find_novel(self._novels, ident)

    def reload(self) -> None:
        """重新读 data/config.json 与 data/novels.json：手工改完 selector 后托盘点「重新加载配置」即可生效。"""
        with self._lock:
            self._cfg = load_config()
            self._rules = self._cfg.compiled()
            self._novels = load_novels(self.paths)
            if not self.paths.novels.exists() and self._novels:
                # 读过一次 v1 书架：马上按 v2 落盘，之后手改只有 novels.json 一处
                save_novels(self.paths, self._novels)
            self._fetcher = Fetcher(
                user_agent=self._cfg.user_agent,
                timeout=self._cfg.request_timeout_seconds,
                retries=self._cfg.request_retries,
                backoff=self._cfg.retry_backoff_seconds,
                max_bytes=self._cfg.max_response_bytes,
            )

    def save_novels(self) -> None:
        with self._lock:
            save_novels(self.paths, self._novels)

    def save_config(self) -> None:
        with self._lock:
            save_config(self._cfg)

    def add_novel(self, url: str) -> Novel:
        """新添一本：小说 id 沿用首个来源的 id，老书架里的 id 因此不会变。"""
        url = assert_http_url(url)
        sid = source_id_for(url)
        with self._lock:
            if self._source_by_id(sid) is not None:
                raise DuplicateSource(f"这个目录页已在书架里（id={sid}）")
            novel = Novel(id=sid, title="", sources=[Source(id=sid, toc_url=url,
                                                            site=host_label(url))],
                          primary_id=sid, created_at=now_stamp())
            self._novels.append(novel)
            return novel

    def add_source(self, novel_ident: str, url: str) -> Source:
        """给已有的一本再加一个来源：站点、繁简、代理都可以不同。"""
        url = assert_http_url(url)
        sid = source_id_for(url)
        with self._lock:
            if self._source_by_id(sid) is not None:
                raise DuplicateSource(f"这个目录页已在书架里（id={sid}）")
            novel = find_novel(self._novels, novel_ident)
            if novel is None:
                raise ValueError(f"找不到《{novel_ident}》")
            source = Source(id=sid, toc_url=url, site=host_label(url), created_at=now_stamp())
            novel.sources.append(source)
            if not novel.title and source.title:
                novel.title = source.title
            return source

    def _source_by_id(self, sid: str) -> Source | None:
        for novel in self._novels:
            for source in novel.sources:
                if source.id == sid:
                    return source
        return None

    def remove_novel(self, ident: str) -> Novel | None:
        with self._lock:
            novel = find_novel(self._novels, ident)
            if novel is None:
                return None
            self._novels = [n for n in self._novels if n.id != novel.id]
            return novel

    def remove_source(self, novel_ident: str, source_ident: str) -> Source | None:
        """删掉最后一个来源等于把整本移出书架。"""
        with self._lock:
            novel = find_novel(self._novels, novel_ident)
            if novel is None:
                return None
            source = novel.source(source_ident)
            if source is None:
                return None
            novel.sources = [s for s in novel.sources if s.id != source.id]
            if not novel.sources:
                self._novels = [n for n in self._novels if n.id != novel.id]
            elif novel.primary_id == source.id:
                novel.primary_id = novel.sources[0].id
            return source

    def set_primary(self, novel_ident: str, source_ident: str) -> Source | None:
        """换主源：只改「主源未更新」这类提示的口径，抓取照样每个来源都跑。"""
        with self._lock:
            novel = find_novel(self._novels, novel_ident)
            source = novel.source(source_ident) if novel else None
            if source is not None:
                novel.primary_id = source.id
                save_novels(self.paths, self._novels)
            return source

    def set_source_enabled(self, novel_ident: str, source_ident: str, enabled: bool) -> Source | None:
        with self._lock:
            novel = find_novel(self._novels, novel_ident)
            source = novel.source(source_ident) if novel else None
            if source is not None:
                source.enabled = enabled
                save_novels(self.paths, self._novels)
            return source

    def set_novel_enabled(self, ident: str, enabled: bool) -> Novel | None:
        with self._lock:
            novel = find_novel(self._novels, ident)
            if novel is not None:
                for source in novel.sources:
                    source.enabled = enabled
                save_novels(self.paths, self._novels)
            return novel

    def set_poll_minutes(self, value: int | str) -> None:
        with self._lock:
            self._cfg.poll_minutes = value
            save_config(self._cfg)

    # ---------- 已读标记 ----------

    def mark_read(self, ident: str, chapter: Chapter) -> bool:
        """记一章为已读：托盘里点「打开最新章节」时直接写，卡片则走 read.jsonl。

        已读记在小说级、用跨源 match_key，所以在一边点开过，另一边也显示灰色。
        """
        key = match_key(chapter.number, chapter.title)
        with self._lock:
            novel = find_novel(self._novels, ident)
            if novel is None or key in novel.read_keys:
                return False
            self._remember_read(novel, key)
            save_novels(self.paths, self._novels)
            return True

    def _remember_read(self, novel: Novel, key: str) -> None:
        novel.read_keys.append(key)
        cap = self._cfg.notified_keys_keep
        if len(novel.read_keys) > cap:
            del novel.read_keys[:-cap]

    def _absorb_read_marks(self) -> int:
        """卡片进程把点开过的章节写进 data/read.jsonl，这里吸收进书架。调用方需已持锁。"""
        records = drain_jsonl(self.paths.read_log)
        if not records:
            return 0
        by_id = {n.id: n for n in self._novels}
        changed = 0
        for record in records:
            novel = by_id.get(str(record.get("novel") or ""))
            key = str(record.get("key") or "")
            if novel is None or not key or key in novel.read_keys:
                continue
            self._remember_read(novel, key)
            changed += 1
        if changed:
            save_novels(self.paths, self._novels)
        return changed

    # ---------- 检查 ----------

    def set_fetcher(self, fetcher) -> None:
        """替换抓取器：测试注入假站点，或需要换代理/自定义会话时使用。"""
        with self._lock:
            self._fetcher = fetcher

    def check_one(self, ident: str, notify: bool = True) -> list[Outcome]:
        """检查一本书的所有来源：返回值按来源顺序，一本小说的多个来源一起看。"""
        with self._lock:
            self._absorb_read_marks()
            novel = find_novel(self._novels, ident)
            if novel is None:
                return []
            outcomes: list[Outcome] = []
            previous_host = ""
            for source in novel.sources:
                host = site_key(source.toc_url)
                if previous_host and host and host == previous_host:
                    # 同一本书的多个来源常挂在同一个域/同一个反代上，串起来打，别并发敲门
                    sleep_seconds(self._rules.cfg.same_host_delay_seconds)
                previous_host = host
                outcomes.append(check_source(novel, source, self._rules, self._fetcher, self.paths))
        for outcome in outcomes:
            self._log(outcome)
        self._collect([novel], outcomes, notify)
        return outcomes

    def run_round(self, skip_ids: set[str] | None = None, notify: bool = True) -> list[Outcome]:
        with self._lock:
            self._absorb_read_marks()
            novels = list(self._novels)
            rules = self._rules
            fetcher = self._fetcher
        if not novels:
            return []
        results = check_all(novels, rules, fetcher, self.paths, skip_ids=skip_ids)
        ordered = [results[s.id] for n in novels for s in n.sources if s.id in results]
        for outcome in ordered:
            self._log(outcome)
        self._collect(novels, ordered, notify)
        return ordered

    def _collect(self, novels: list[Novel], outcomes: list[Outcome], notify: bool) -> list[NovelAlert]:
        """把「各来源各自的新章」压成「这本书要提醒什么」，然后每本一张卡片。

        去重要在没有卡片的那一轮也做：`--no-toast` 只是不弹，不代表下一章以后还能再报一遍。
        """
        with self._lock:
            alerts = collect_alerts(novels, outcomes, keep=self._rules.cfg.notified_keys_keep)
            save_novels(self.paths, self._novels)
            self.last_alerts = alerts
        if notify:
            Notifier(self.paths.alerts_log).notify_many(alerts)
        return alerts

    def _log(self, outcome: Outcome) -> None:
        append_text(
            self.paths.runtime_log,
            f"{now_stamp()} [{status_label(outcome.status)}] {outcome.summary()}",
        )

    # ---------- 汇总 ----------

    def history(self, ident: str, limit: int = 20) -> list[dict]:
        """一本书的更新轨迹：把各来源的记录按时间倒序合在一张清单里。"""
        novel = self.novel(ident)
        if novel is None:
            return []
        records: list[dict] = []
        for source in novel.sources:
            for record in read_jsonl(self.paths.source_history(novel.id, source.id), limit=limit):
                merged = dict(record)
                merged["site"] = record.get("site") or source.site
                records.append(merged)
        records.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
        return records[:limit]

    def startup_lines(self, outcomes: list[Outcome]) -> list[str]:
        """启动提示的内容：无更新只报最后一章，有更新逐章列标题。"""
        if not outcomes:
            return ["书架为空：复制小说目录页链接后用托盘「添加小说（从剪贴板）」"]
        lines = [f"共监控 {len({o.novel.id for o in outcomes})} 本 {len(outcomes)} 个来源"]
        for outcome in outcomes:
            title = outcome.novel.display_title()
            if outcome.status == STATUS_UPDATED:
                names = " / ".join(c.title for c in outcome.new_chapters)
                more = f"（共 {outcome.total_new} 章）" if outcome.total_new > len(outcome.new_chapters) else ""
                lines.append(f"《{title}》{outcome.source.site} 有更新，{outcome.total_new} 章{more}：{names}")
            elif outcome.status == STATUS_BASELINE:
                lines.append(f"《{title}》{outcome.source.site} 首次记录：最后一章 {outcome.source.latest_title or '未知'}")
            elif outcome.status == STATUS_NO_CHANGE:
                lines.append(f"《{title}》{outcome.source.site} 无更新：最后一章 {outcome.source.latest_title or '未知'}")
            else:
                lines.append(f"《{title}》{outcome.source.site} {outcome.detail()}")
        return lines

    def toast_startup(self, outcomes: list[Outcome]) -> None:
        """命令行调试时也能弹出启动摘要。"""
        lines = [line for line in self.startup_lines(outcomes)[1:]]
        launch = next((o.latest.url for o in outcomes if o.latest), "")
        Notifier(self.paths.alerts_log).show("顺手 · 启动检查", lines, launch)
