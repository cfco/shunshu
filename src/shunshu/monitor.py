"""单来源检查与并发检查：基线、去重、多章更新、目录重置判定都在这里。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .config import Rules
from .align import match_key
from .http import FetchError, Fetcher, ProxyError
from .models import (
    STATUS_BASELINE,
    STATUS_CHECK_ERROR,
    STATUS_DISABLED,
    STATUS_NET_ERROR,
    STATUS_NO_CHANGE,
    STATUS_PARSE_FAILED,
    STATUS_PROXY_ERROR,
    STATUS_TOC_RESET,
    STATUS_UPDATED,
    Chapter,
    ChapterEntry,
    Novel,
    NovelAlert,
    Outcome,
    Source,
    normalize_chapter_url,
    parse_chapter_number,
    site_key,
)
from .reader import LoadResult, load_toc
from .store import (
    Paths,
    append_jsonl,
    append_text,
    log_filtered,
    now_stamp,
    sleep_seconds,
    write_candidates,
)
from .toc_detect import TocDetectError


def check_source(novel: Novel, source: Source, rules: Rules, fetcher: Fetcher,
                 paths: Paths | None = None, *, skip: bool = False,
                 prefetch: str | None = None) -> Outcome:
    if not source.enabled:
        return Outcome(novel=novel, source=source, status=STATUS_DISABLED, message="已暂停",
                       latest=source.latest_chapter())
    if skip:
        return Outcome(novel=novel, source=source, status=source.status,
                       message=f"连续失败退避中（{source.consecutive_failures} 次）",
                       latest=source.latest_chapter())

    try:
        loaded = load_toc(fetcher, source.toc_url, rules, selector=source.selector,
                          proxy=source.proxy, html=prefetch, paths=paths)
    except ProxyError as exc:
        return _failure(novel, source, STATUS_PROXY_ERROR, f"代理不可用：{exc}",
                        exc=str(exc), fatal_count=True)
    except FetchError as exc:
        return _failure(novel, source, STATUS_NET_ERROR, f"网络错误：{exc}", exc=str(exc), fatal_count=True)
    except TocDetectError as exc:
        if paths is not None:
            write_candidates(paths, novel, source, exc.candidates, note=str(exc))
        hint = f"解析失败：{exc}（候选容器已写入 data/candidates/{source.id}.txt）"
        return _failure(novel, source, STATUS_PARSE_FAILED, hint, exc=str(exc), fatal_count=True)
    except ValueError as exc:
        return _failure(novel, source, STATUS_PARSE_FAILED, str(exc), exc=str(exc))

    chapters = _apply_manual_keys(source, loaded)
    if not chapters:
        return _failure(novel, source, STATUS_PARSE_FAILED, "过滤后没有剩余章节，疑似整站改版",
                        exc="empty after filter")

    if paths is not None:
        log_filtered(paths, source, [(d.reason, d.text, d.url) for d in loaded.dropped])

    source.consecutive_failures = 0
    source.last_error = None
    known = source.known_keys()
    keys_now = {c.key for c in chapters}
    new = [c for c in chapters if c.key not in known]

    if not source.baseline_set or not source.latest_url:
        return _baseline(novel, source, loaded, chapters, rules, paths)

    # 分母用「上一轮目录里的章数」，不是累计 notified_keys：滚动式目录只列最近十几章，
    # 而 notified_keys 会把离开目录的老章再留最多 500 条，拿它当分母 overlap 只会一路下跌，
    # 跌破阈值后每轮都判「疑似改版」→ 这本书从此再不提醒，且托盘只有一行灰字
    previous = source.chapter_count or len(known)
    still_there = len(known & keys_now)
    overlap = min(1.0, still_there / previous) if previous else 1.0
    if previous >= 5 and overlap < rules.cfg.toc_reset_overlap_ratio:
        why = (f"上一轮目录 {previous} 章里只剩 {still_there} 章还在，"
               "目录疑似被重置或整站改版，已记录不提醒")
        return _reset(novel, source, loaded, chapters, new, rules, paths, why)

    if not new:
        return _no_change(novel, source, loaded, chapters, rules, paths)

    # 这一轮多跟到了一页（比如站点只挂「下一页」而我们刚开始跟），冒出来的成章新章是真新章，
    # 不是重编号，不能按「一次太多新章节」判成目录重置
    grew_pages = loaded.toc.pages_followed > source.pages_followed
    too_many = (len(new) > rules.cfg.max_report_new_chapters
                or len(new) > len(chapters) * rules.cfg.toc_reset_new_ratio)
    # 关机几天或被退避跳过几轮之后回来，一次追平 31+ 章是常态：新章编号全部紧接在
    # 已知的最新章之后就是断档追赶，照报。原来这里直接判重编号并 _commit 记账，
    # 那批章被写进「已提醒过」，一条没报就永久静默（用户最在意的漏提醒）
    if too_many and not grew_pages and not _continues_sequence(new, source.latest_number):
        return _reset(novel, source, loaded, chapters, new, rules, paths,
                      f"一次出现 {len(new)} 个新章节标识，疑似章节重编号，已记录不提醒")

    latest = chapters[-1]
    _commit(novel, source, loaded, chapters, latest, rules=rules)
    source.status = STATUS_UPDATED
    source.status_detail = f"新增 {len(new)} 章"
    display = new[-rules.cfg.history_display_cap:]
    _history(paths, novel, source, "update", new, latest)
    return Outcome(novel=novel, source=source, status=STATUS_UPDATED, new_chapters=display,
                   new_count=len(new), latest=latest, message=f"更新 {len(new)} 章",
                   all_new=new)


def check_source_safe(novel: Novel, source: Source, rules: Rules, fetcher: Fetcher,
                      paths: Paths | None = None, *, skip: bool = False,
                      prefetch: str | None = None) -> Outcome:
    """`check_source` 的兜底外壳：连未预期的异常也归成这一个来源的失败结果。

    `check_source` 只截得住它认得的那几类错误。别的一个异常（解析层崩了、来源的某个字段
    被手改成没法用的形状）从工作线程逃出去，就会让 `check_all` 的 `f.result()` 重抛，
    整轮的 `_collect` 不再执行——那一轮已经写进来源级 `notified_keys` 的章节就此永远不再
    提醒，托盘还在、日志还在写，看不出来。宁可让这一个来源记一次失败，也不放弃整轮记账。
    """
    try:
        return check_source(novel, source, rules, fetcher, paths, skip=skip, prefetch=prefetch)
    except Exception as exc:  # 兜底要兜的就是「不知道会抛什么」这一类
        detail = f"检查异常：{type(exc).__name__}: {exc}"
        if paths is not None:
            append_text(paths.runtime_log, f"{now_stamp()} 来源 {source.id} {detail}")
        return _failure(novel, source, STATUS_CHECK_ERROR, detail, exc=detail, fatal_count=True)


def _failure(novel: Novel, source: Source, status: str, message: str, *, exc: str | None = None,
             fatal_count: bool = False) -> Outcome:
    if fatal_count:
        source.consecutive_failures += 1
    source.last_error = exc or message
    source.last_checked = now_stamp()
    source.status = status
    source.status_detail = message
    return Outcome(novel=novel, source=source, status=status, message=message,
                   latest=source.latest_chapter(), error=exc or message)


def _baseline(novel: Novel, source: Source, loaded: LoadResult, chapters: list[Chapter],
              rules: Rules, paths: Paths | None) -> Outcome:
    latest = chapters[-1]
    _commit(novel, source, loaded, chapters, latest, rules=rules)
    source.baseline_set = True
    source.status = STATUS_BASELINE
    source.status_detail = "首次记录，未提醒"
    # 刚添加时要把「最近 5 章」弹出来给人确认认对了书，所以基线也带一份快照
    recent = list(reversed(chapters))[:rules.cfg.history_display_cap]
    _history(paths, novel, source, "baseline", [], latest)
    return Outcome(novel=novel, source=source, status=STATUS_BASELINE, recent=recent,
                   latest=latest, message="首次记录（不提醒）",
                   known_keys=[match_key(c.number, c.title) for c in chapters])


def _no_change(novel: Novel, source: Source, loaded: LoadResult, chapters: list[Chapter],
               rules: Rules, paths: Paths | None) -> Outcome:
    latest = chapters[-1]
    _commit(novel, source, loaded, chapters, latest, rules=rules)
    source.status = STATUS_NO_CHANGE
    source.status_detail = "无更新"
    return Outcome(novel=novel, source=source, status=STATUS_NO_CHANGE, latest=latest, message="无更新")


def _continues_sequence(new: list[Chapter], latest_number: int | None) -> bool:
    """新章编号是否全部紧接在已知的最新章之后（=断档追赶，不是整站重编号）。

    任何一章编号缺失或比已知最新章还小就返回 False，维持「只记录不提醒」的保守口径——
    宁可少报一次改版误报，也不能把真断档当重编号吞掉，那是永久漏提醒。
    """
    if latest_number is None or not new:
        return False
    return all(chapter.number is not None and chapter.number > latest_number for chapter in new)


def _reset(novel: Novel, source: Source, loaded: LoadResult, chapters: list[Chapter],
           new: list[Chapter], rules: Rules, paths: Paths | None, why: str) -> Outcome:
    """目录重置/重编号：只更新状态和记录，不弹窗，避免整站改版刷屏。"""
    latest = chapters[-1]
    _commit(novel, source, loaded, chapters, latest, rules=rules)
    source.status = STATUS_TOC_RESET
    source.status_detail = why
    source.last_error = None
    _history(paths, novel, source, "toc_reset", new[:50], latest)
    return Outcome(novel=novel, source=source, status=STATUS_TOC_RESET, latest=latest, message=why)


def _commit(novel: Novel, source: Source, loaded: LoadResult, chapters: list[Chapter],
            latest: Chapter, *, rules: Rules) -> None:
    current = [c.key for c in chapters]
    current_set = set(current)
    retained = [k for k in source.notified_keys if k not in current_set]
    # 目录里还列着的每一章都必须留在账上：上千章的书一旦被上限裁掉，下一轮会把
    # 这些老章节全当成新章节，永远在「疑似重编号」里出不来。上限只裁已经不在目录里的历史 key。
    tail = max(0, rules.cfg.notified_keys_keep)
    history = retained[-tail:] if tail else []
    source.notified_keys = list(dict.fromkeys([*current, *history]))
    source.latest_title = latest.title
    source.latest_url = latest.url
    source.latest_number = latest.number
    source.chapter_count = len(chapters)
    source.pages_followed = loaded.toc.pages_followed
    source.last_checked = now_stamp()
    if loaded.toc.book_title:
        if not source.title:
            source.title = loaded.toc.book_title
        # 书名以最先抓到的一本为准，繁简差异由显示层统一，不再回写
        if not novel.title:
            novel.title = loaded.toc.book_title


def _apply_manual_keys(source: Source, loaded: LoadResult) -> list[Chapter]:
    """keep_keys 把被过滤掉的链接捞回来，drop_keys 把漏网的广告再杀掉。"""
    keep = {k.strip() for k in source.keep_keys if k.strip()}
    drop = {k.strip() for k in source.drop_keys if k.strip()}
    chapters = {c.key: c for c in loaded.toc.chapters}

    # 捞回的先收在一边，删完 drop 再插回目录原位。不能直接挂到末尾：_commit 取
    # chapters[-1] 当「最新章节」，捞回一章旧的就把它算成最新章节，latest_url /
    # 托盘「打开最新章节」/ 基线快照全跟着错。
    restored: list[Chapter] = []
    for item in loaded.dropped:
        if item.reason in ("bad_scheme", "bad_href") or not item.text:
            continue
        key = normalize_chapter_url(item.url)
        if key in chapters:
            continue
        if key in keep or item.text in keep or item.url in keep:
            restored.append(Chapter(title=item.text, url=item.url,
                                    number=parse_chapter_number(item.text)))

    for key, chapter in list(chapters.items()):
        if key in drop or chapter.title in drop or chapter.url in drop:
            del chapters[key]

    ordered = list(chapters.values())
    for chapter in restored:
        ordered = _insert_by_number(ordered, chapter)
    return ordered


def _insert_by_number(chapters: list[Chapter], chapter: Chapter) -> list[Chapter]:
    """把捞回的章节按编号插回原位：停在本章之前（末尾编号都比它小就追加到末尾）。

    只在有编号时按编号插。编号读不出来的站（站点编号本来就乱）只能追加到末尾——
    那种站「哪章最新」本来就不靠编号判断，硬猜反而更糟。
    """
    if chapter.number is None:
        return [*chapters, chapter]
    for index, existing in enumerate(chapters):
        if existing.number is not None and existing.number > chapter.number:
            return [*chapters[:index], chapter, *chapters[index:]]
    return [*chapters, chapter]


def _history(paths: Paths | None, novel: Novel, source: Source, kind: str,
             chapters: list[Chapter], latest: Chapter) -> None:
    if paths is None:
        return
    append_jsonl(paths.source_history(novel.id, source.id), {
        "ts": now_stamp(),
        "kind": kind,
        "novel": novel.title or novel.id,
        "site": source.site,
        "latest": {"t": latest.title, "u": latest.url},
        "chapters": [c.to_dict() for c in chapters],
    })


def check_all(novels: list[Novel], rules: Rules, fetcher: Fetcher, paths: Paths,
              skip_ids: set[str] | None = None) -> dict[str, Outcome]:
    """同域名串行、不同域名并发：既快又不至于把某个站并发打穿。"""
    skip_ids = skip_ids or set()
    groups: dict[str, list[tuple[Novel, Source]]] = {}
    for novel in novels:
        for source in novel.sources:
            groups.setdefault(site_key(source.toc_url) or urlsplit(source.toc_url).netloc, []) \
                .append((novel, source))
    if not groups:
        return {}

    items = list(groups.items())
    if len(items) == 1:
        outcomes = _check_group(items[0][1], items[0][0], rules, fetcher, paths, skip_ids)
    else:
        workers = max(1, min(rules.cfg.concurrency, len(items)))
        # 真 Fetcher 提供常驻池：工作线程与线程内 Session/keep-alive 跨轮复用；
        # 测试注入的假抓取器没有 pool()，退回本轮临时池（用完即关，不留空闲线程）
        pool_factory = getattr(fetcher, "pool", None)
        pool = pool_factory(workers) if pool_factory is not None else ThreadPoolExecutor(
            max_workers=workers)
        try:
            futures = [
                pool.submit(_check_group, group, host, rules, fetcher, paths, skip_ids)
                for host, group in items
            ]
            outcomes = [o for group in (f.result() for f in futures) for o in group]
        finally:
            if pool_factory is None:
                pool.shutdown(wait=False)
    return {o.source.id: o for o in outcomes}


def _check_group(items: list[tuple[Novel, Source]], host: str, rules: Rules, fetcher: Fetcher,
                 paths: Paths, skip_ids: set[str]) -> list[Outcome]:
    out: list[Outcome] = []
    for index, (novel, source) in enumerate(items):
        if index:
            sleep_seconds(rules.cfg.same_host_delay_seconds)
        out.append(check_source_safe(novel, source, rules, fetcher, paths,
                                     skip=source.id in skip_ids))
    return out


_INDEX_ROWS = 50
_ALERTED_KEYS_KEEP = 500


def _chapter_rank(chapter: Chapter, count: int) -> tuple[int, int]:
    """跨来源混排用的「新旧」序：离自己目录末尾越近越新，一样近时章节号大的在前。

    比的是「离自己目录末尾多远」而不是目录名次——名次每个来源各编各的：主源 1000 章
    里的第 62 章名次 999，只列最近 20 章的镜像里第 63 章名次 19，直接比名次就会把旧的
    那章排在前面，卡片首行不再是最新章。
    """
    rel_from_end = (count - 1 - chapter.position) if count else -chapter.position
    number = chapter.number if chapter.number is not None else -1
    return (rel_from_end, -number)


def collect_alerts(novels: list[Novel], outcomes: list[Outcome], *, keep: int = _ALERTED_KEYS_KEEP,
                   index_rows: int = _INDEX_ROWS) -> list[NovelAlert]:
    """把「每个来源各自的新章」压成「这本小说真正要提醒的章节」。

    同一章只提醒一次：谁先报就归谁（索引里记下那个来源），后来的来源再报同一个
    `match_key` 也不重复弹卡片。基线那批章节直接算「已经知道的了」，所以慢半拍的
    镜像站不会把外面早更了几周的章当新章节报一遍。
    """
    by_source = {o.source.id: o for o in outcomes}
    alerts: list[NovelAlert] = []
    for novel in novels:
        own = [by_source[s.id] for s in novel.sources if s.id in by_source]
        if not own:
            continue
        seen = set(novel.alerted_keys)
        candidates: list[tuple[tuple[int, int], ChapterEntry]] = []
        for outcome in own:
            if outcome.status != STATUS_UPDATED:
                continue
            count = outcome.source.chapter_count or 0
            for chapter in outcome.all_new:
                key = match_key(chapter.number, chapter.title)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((_chapter_rank(chapter, count), ChapterEntry(
                    k=key, t=chapter.title, u=chapter.url,
                    s=outcome.source.id, n=chapter.number, ts=now_stamp())))
        # 离末尾越近越新；两个来源各报一章、离末尾一样近时按章节号分先后
        fresh = [entry for _, entry in sorted(candidates, key=lambda pair: pair[0])]

        baselines = [o for o in own if o.status == STATUS_BASELINE]
        for outcome in baselines:
            novel.mark_alerted(outcome.known_keys, keep=keep)

        added = not novel.chapter_index and bool(baselines)
        if added:
            # 一本新书可能同时挂好几个来源（手改 novels.json 一次写进两个，或启动首轮
            # 一起建档）：各来源的「最近几章」都要收进索引，按 `_chapter_rank` 混排去重，
            # 卡片才列得出最新那 5 章。只取 baselines[0] 会让其余来源的章节从索引里消失，
            # 之后某轮它们冒出来会被当成新章报一遍。
            picked: dict[str, tuple[tuple[int, int], ChapterEntry]] = {}
            for outcome in baselines:
                count = outcome.source.chapter_count or 0
                for chapter in outcome.recent:
                    key = match_key(chapter.number, chapter.title)
                    if key in picked:
                        continue
                    picked[key] = (_chapter_rank(chapter, count), ChapterEntry(
                        k=key, t=chapter.title, u=chapter.url,
                        s=outcome.source.id, n=chapter.number, ts=now_stamp()))
            fresh = [entry for _, entry in sorted(picked.values(), key=lambda pair: pair[0])]
            latest = novel.latest_chapter()
        elif fresh:
            # `mark_alerted` 按「列表尾部＝最近」裁上限，而 `fresh` 是新→旧，正着传进去会让
            # 刚弹过卡的最新章节最先被挤掉。将来会重复报上来的恰恰是这一批（慢半拍的镜像
            # 从自己目录的末尾追上来），所以账本要留最新那几章：一次补更上千章、超过了
            # notified_keys_keep 时，镜像补报同一批不再重新弹卡。
            novel.mark_alerted([e.k for e in reversed(fresh)], keep=keep)
            latest = novel.latest_chapter()
        else:
            continue

        novel.add_to_index(fresh, rows=index_rows)
        alerts.append(NovelAlert(novel=novel, entries=fresh, added=added, latest=latest))
    return alerts
