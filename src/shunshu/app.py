"""应用状态：书架增删改 + 跑一轮检查 + 落盘，托盘和命令行共用。"""

from __future__ import annotations

import threading
from urllib.parse import urlsplit

from .align import match_key
from .config import Config, Rules, load_config, save_config
from .http import Fetcher, assert_http_url, proxy_label
from .models import (
    STATUS_PROXY_ERROR,
    Chapter,
    Novel,
    NovelAlert,
    Outcome,
    Source,
    host_label,
    site_key,
    status_label,
    strip_www,
)
from .monitor import check_all, check_source_safe, collect_alerts
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
        # 整轮级互斥：check_one（添加书源的首轮检查）与 run_round（定时轮）不同时抓取，
        # 避免同一个来源被两条线程同时抓、状态互相覆盖。与 AppState._lock 分开：
        # 本锁跨网络请求持有，_lock 只保护数据结构
        self._round_lock = threading.Lock()
        self.last_alerts: list[NovelAlert] = []
        # 整个进程共用一个 Notifier：卡片槽位是按「最近 12.5 秒里占了几个位置」算的，
        # 每轮新建一份就等于每轮都从 0 开始数，托盘回执与更新卡全挤在同一个位置
        self.notifier = Notifier(self.paths.alerts_log)
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
        """重新读 data/config.json 与 data/novels.json：手工改完 selector 后托盘点「重新加载配置」即可生效。

        配置/书架 JSON 损坏时读取层会隔离备份并降级（默认配置/空书架），具体原因放在
        self.load_warnings，由托盘弹卡告知，不让整个工具起不来。
        """
        from .store import LAST_LOAD_WARNINGS

        # 整轮级互斥：换 fetcher / 换 novels 必须与进行中的一轮串行。否则 reload 会在某轮
        # 抓取途中 close() 掉它正攥着的连接池（pool.submit 抛 RuntimeError 掀掉整轮），或让
        # 该轮把状态改到已被替换掉的旧 novel 对象上（存盘写的是新书架，这一轮记账丢失、甚至
        # 下一章重复弹卡）。reload 已在后台线程执行，这里等一轮跑完再换可接受；锁序
        # _round_lock→_lock 与 run_round/check_one 一致，不会死锁。
        with self._round_lock:
            with self._lock:
                LAST_LOAD_WARNINGS.clear()
                self._cfg = load_config()
                self._rules = self._cfg.compiled()
                self._novels = load_novels(self.paths)
                old_fetcher = getattr(self, "_fetcher", None)
                self._fetcher = Fetcher(
                    user_agent=self._cfg.user_agent,
                    timeout=self._cfg.request_timeout_seconds,
                    retries=self._cfg.request_retries,
                    backoff=self._cfg.retry_backoff_seconds,
                    max_bytes=self._cfg.max_response_bytes,
                    workers=self._cfg.concurrency,
                )
                self.load_warnings = list(LAST_LOAD_WARNINGS)
            # 旧抓取器的常驻连接池在 _lock 外、但仍在 _round_lock 内释放：此刻没有一轮在跑，
            # close() 不会打断进行中的抓取。wait=False 且不取消，已排队的任务让它自然跑完，
            # 见 Fetcher.close
            if old_fetcher is not None:
                close = getattr(old_fetcher, "close", None)
                if close is not None:
                    close()

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
            # 新挂来源不写书名：Source.title 创建时恒为空，取名留给首轮抓取（monitor._commit）
            source = Source(id=sid, toc_url=url, site=host_label(url), created_at=now_stamp())
            novel.sources.append(source)
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

    def set_pause_enabled(self, enabled: bool) -> None:
        """托盘「暂停检查」开关只翻 pause_enabled；时段两端时间仍在 config.json 里改。"""
        with self._lock:
            self._cfg.pause_enabled = enabled
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
        if cap <= 0:
            # cap=0 表示一个都不留：del [:-0] 等于 del [:0] 一条都删不掉，必须显式清空
            novel.read_keys.clear()
        elif len(novel.read_keys) > cap:
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

    # ---------- 代理自动探测 ----------

    @staticmethod
    def _site_host(toc_url: str) -> str:
        """站点代理记忆的 key：完整主机名去掉 www 前缀（m.book.example 这种子域保留）。"""
        return strip_www(urlsplit(toc_url).hostname or "")

    def ensure_source_proxy(self, source_id: str, fetcher=None, *,
                            force: bool = False):
        """确认这个站该直连还是走代理，结论写进配置后后续每轮照用。

        判定顺序：
        1. 来源手写的 proxy（novels.json，且与站点记忆不一致）→ 永远原样尊重，不探测；
        2. 非 force 且 config.site_proxies 里已有该站记忆（含「直连」空串）→ 直接套用；
        3. 其余情况 → 先直连，连不上再试本机代理，成功才把结论写进 config 和该来源。
        全部不通返回 None（不记忆，下次还会再试）。探测在锁外执行，避免卡住菜单。

        force=True 用于代理连续失败后的自动重探：忽略旧记忆重新探一遍，以便用户换了
        代理客户端（7890 换成 10809）后自动愈合；手写代理仍然不覆盖。
        返回值是 proxy_detect.ProbeResult（含探测时抓回的首页 HTML 供首轮复用）。
        """
        from .proxy_detect import ProbeResult, make_probe_fetcher, probe_url

        with self._lock:
            source = self._source_by_id(source_id)
            if source is None:
                return None
            current = source.proxy.strip()
            host = self._site_host(source.toc_url)
            remembered = self._cfg.site_proxies.get(host)
            # 与站点记忆相同的代理是当初自动写上去的，force 重探允许改；只有「自己写的」
            # （非空且不等于记忆）才算手写代理，永远不被自动探测覆盖
            manual = current and current != remembered
            if manual:
                # 尊重手写覆盖：一个包都没试，probed=False 让调用别说成「探出来是这个」
                return ProbeResult(proxy=current, probed=False)
            if not force and remembered is not None:
                source.proxy = remembered
                save_novels(self.paths, self._novels)
                return ProbeResult(proxy=remembered, probed=False)
            user_agent = self._cfg.user_agent
            max_bytes = self._cfg.max_response_bytes
            toc_url = source.toc_url

        probe = fetcher or make_probe_fetcher(user_agent, max_bytes)
        result = probe_url(probe, toc_url)
        if result is None:
            return None

        with self._lock:
            # 探测期间用户可能点了「重新加载配置」：以锁内最新对象再定位一次
            source = self._source_by_id(source_id)
            host = self._site_host(toc_url)
            self._cfg.site_proxies[host] = result.proxy
            if source is not None:
                source.proxy = result.proxy
            save_config(self._cfg)
            save_novels(self.paths, self._novels)
        return result

    def _reprobe_dead_proxies(self, outcomes: list[Outcome]) -> None:
        """代理连续失败到退避阈值时自动重探一次，让「换了代理客户端」能自愈。

        只在 consecutive_failures 恰好涨到阈值的这一轮触发：仍失败则计数继续涨，
        不会每轮都重探；恢复成功后计数清零，将来再坏到阈值还能再来一次。

        只有真敲过门的结果才清失败计数：手写代理走的是「原样尊重」那条分支，根本没探，
        把它当「重探成功」会让死代理每 5 轮被清零一次、永远退避不起来，日志还会谎报改走了新代理。
        """
        threshold = self._cfg.failure_backoff_threshold
        for outcome in outcomes:
            if outcome.status != STATUS_PROXY_ERROR:
                continue
            if outcome.source.consecutive_failures != threshold:
                continue
            result = self.ensure_source_proxy(outcome.source.id, force=True)
            if result is None or not result.probed:
                continue
            with self._lock:
                source = self._source_by_id(outcome.source.id)
                if source is not None:
                    source.consecutive_failures = 0
                    source.last_error = None
            append_text(
                self.paths.runtime_log,
                f"{now_stamp()} 来源 {outcome.source.site} 代理已失效，自动重探改走："
                f"{proxy_label(result.proxy) or '直连'}",
            )

    # ---------- 检查 ----------

    def set_fetcher(self, fetcher) -> None:
        """替换抓取器：测试注入假站点，或需要换代理/自定义会话时使用。"""
        with self._lock:
            old = self._fetcher
            self._fetcher = fetcher
        if old is not None and old is not fetcher:
            close = getattr(old, "close", None)
            if close is not None:
                close()

    def check_one(self, ident: str, notify: bool = True,
                  prefetch: dict[str, str] | None = None) -> list[Outcome]:
        """检查一本书的所有来源：返回值按来源顺序，一本小说的多个来源一起看。

        照 run_round 的「锁内取快照、锁外抓取」模式：网络请求（含同主机间隔 sleep）
        不能持着全局锁做，否则托盘菜单在整本书抓完前完全卡死。
        prefetch 形如 {来源id: 已抓回的首页 HTML}，添加书源时把代理探测那一下的
        响应透传过来复用，少敲一次门。
        """
        # 整轮级互斥：定时轮正在跑时这里等它跑完再抓，同一来源绝不被两条线程同时抓
        with self._round_lock:
            with self._lock:
                self._absorb_read_marks()
                novel = find_novel(self._novels, ident)
                if novel is None:
                    return []
                plan = [(source, site_key(source.toc_url)) for source in novel.sources]
                rules = self._rules
                fetcher = self._fetcher
            prefetch = prefetch or {}
            outcomes: list[Outcome] = []
            previous_host = ""
            for source, host in plan:
                if previous_host and host and host == previous_host:
                    # 同一本书的多个来源常挂在同一个域/同一个反代上，串起来打，别并发敲门
                    sleep_seconds(rules.cfg.same_host_delay_seconds)
                previous_host = host
                html = prefetch.get(source.id)
                outcomes.append(check_source_safe(novel, source, rules, fetcher, self.paths,
                                                  prefetch=html or None))
            for outcome in outcomes:
                self._log(outcome)
            alerts = self._collect([novel], outcomes)
        # 弹卡放到整轮锁外：spawn 每张卡要等最多 0.5 秒确认没秒退，持锁会把定时轮和
        # 另一次添加堵在 _round_lock 上
        if notify:
            self.notifier.notify_many(alerts)
        # 代理重探要敲网络（最坏能拖到一分钟），放到整轮锁外做：不然用户在重探进行时
        # 添加小说，check_one 会一直等这把锁，添加看起来像卡住了。重探只改 proxy 和
        # 失败计数、不碰抓取状态，与随后开始的下一轮并发也无害。
        self._reprobe_dead_proxies(outcomes)
        return outcomes

    def run_round(self, skip_ids: set[str] | None = None, notify: bool = True) -> list[Outcome]:
        skip_ids = skip_ids or set()
        # 整轮级互斥：与 check_one（添加书源首轮）互斥，同一来源不会被两条线程同时抓
        with self._round_lock:
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
            alerts = self._collect(novels, ordered)
        # 弹卡放到整轮锁外：spawn 每张卡要等最多 0.5 秒确认没秒退，持锁会把「添加小说」的
        # 首轮检查和下一轮都堵在 _round_lock 上（一次大批更新能堵好几秒）
        if notify:
            self.notifier.notify_many(alerts)
        # 定时轮里发现代理彻底失效（到退避阈值）也自动重探一次：放到整轮锁外做，理由同
        # check_one。重探成功会清掉失败计数，调度器随后算退避时自然不再跳过这个来源。
        # 只认真敲过门的来源：被退避跳过的那一轮 outcome 仍带着 proxy_error 状态与到阈值的
        # 失败计数，不过滤会让死代理在每个跳过轮都被重探一次（一次最坏约一分钟）。
        self._reprobe_dead_proxies([o for o in ordered if o.source.id not in skip_ids])
        return ordered

    def _collect(self, novels: list[Novel], outcomes: list[Outcome]) -> list[NovelAlert]:
        """把「各来源各自的新章」压成「这本书要提醒什么」，记账并存盘，返回要弹的卡片。

        只记账、不弹卡：弹卡由调用方放到整轮锁外做（spawn 每张卡要阻塞确认没秒退，
        不能持着 _round_lock）。去重在 notify=False 的那一轮也照做——不弹不代表下一章
        以后还能再报一遍。
        """
        with self._lock:
            alerts = collect_alerts(novels, outcomes, keep=self._rules.cfg.notified_keys_keep)
            save_novels(self.paths, self._novels)
            self.last_alerts = alerts
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
        """启动提示的内容：无更新只报最后一章，有更新逐章列标题。

        逐本那行直接复用 `Outcome.summary()`：「更新 N 章 / 首次记录 / 无更新」这些
        说法只在那一处维护，章节名也统一走简体（`display_title`）。这里只负责在行首
        标明是哪个来源报的——一本书挂多个来源时启动摘要是逐来源一行，得能分清。
        """
        if not outcomes:
            return ["书架为空：复制小说目录页链接后用托盘「添加小说（从剪贴板）」"]
        lines = [f"共监控 {len({o.novel.id for o in outcomes})} 本 {len(outcomes)} 个来源"]
        for outcome in outcomes:
            lines.append(f"{outcome.source.site} · {outcome.summary()}")
        return lines
