"""托盘常驻：图标 + 右键菜单，所有设置都在这一个菜单里。"""

from __future__ import annotations

import os
import threading
import time
from functools import partial

import pystray
from PIL import Image, ImageDraw

from . import autostart
from .align import display_title
from .app import AppState, DuplicateSource
from .browser import open_in_default_browser
from .clipboard import extract_url, read_text
from .config import POLL_MINUTES
from .instance import acquire_singleton
from .http import proxy_label
from .models import (
    STATUS_BASELINE,
    STATUS_CHECK_ERROR,
    STATUS_PARSE_FAILED,
    STATUS_PROXY_ERROR,
    STATUS_UPDATED,
    Novel,
    NovelAlert,
    Source,
    status_label,
)
from .scheduler import Scheduler

_CONFIRM_WINDOW_SECONDS = 10.0

_INTERVAL_LABELS = {5: "5 分钟", 10: "10 分钟", 30: "30 分钟", 60: "60 分钟"}


def make_icon_image(state: str = "normal") -> Image.Image:
    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    palette = {
        "normal": (38, 84, 160, 255),
        "update": (200, 96, 32, 255),
        "error": (170, 40, 40, 255),
    }
    background = palette.get(state, palette["normal"])
    draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=7, fill=background)
    draw.rounded_rectangle((8, 7, 24, 25), radius=2, fill=(245, 245, 240, 255))
    for y in (12, 16, 20):
        draw.line((11, y, 21 if y < 20 else 18, y), fill=background, width=2)
    if state == "update":
        draw.ellipse((20, 20, 30, 30), fill=(240, 60, 60, 255))
    return img


class TrayApp:
    def __init__(self, state: AppState | None = None):
        self.state = state or AppState()
        # 与轮询共用同一个 Notifier：不然托盘回执和更新卡片各自数各自的槽位，永远都从 0 开始
        self.notifier = self.state.notifier
        self.scheduler = Scheduler(self.state, on_round=self._after_round)
        self.icon: pystray.Icon | None = None
        self._pending_delete: tuple[str, float] | None = None
        self._status_hint = "启动中"

    # ---------- 菜单动作 ----------

    def act_add_from_clipboard(self, icon=None, item=None) -> None:
        url = extract_url(read_text())
        if not url:
            self._flash("没有可识别的链接", "请先在浏览器里复制小说目录页的网址")
            return
        try:
            novel = self.state.add_novel(url)
        except DuplicateSource as exc:
            self._flash("已在书架里", str(exc))
            return
        except ValueError as exc:
            self._flash("链接不可用", str(exc))
            return
        self.state.save_novels()
        self._rebuild_menu()
        # 不再单独提示「正在抓取」：首次检查完直接弹「已添加 + 最近 5 章」的卡片，那就是反馈
        self._start_bg(self._check_new_novel, novel.id)

    def act_add_source_from_clipboard(self, novel_id: str, icon=None, item=None) -> None:
        """往已有的一本挂第二个来源：剪贴板里是另一个站的目录页网址。"""
        url = extract_url(read_text())
        if not url:
            self._flash("没有可识别的链接", "请先在浏览器里复制另一个目录页的网址")
            return
        try:
            source = self.state.add_source(novel_id, url)
        except DuplicateSource as exc:
            self._flash("这个网址已在书架里", str(exc))
            return
        except ValueError as exc:
            self._flash("链接不可用", str(exc))
            return
        self.state.save_novels()
        self._rebuild_menu()
        self._start_bg(self._check_new_source, novel_id, source.id)

    def act_reload_config(self, icon=None, item=None) -> None:
        # 读两个 JSON + 新建抓取器要动磁盘和 ssl，放在菜单线程上会把右键菜单顶住一小会儿，
        # 统一丢给后台线程（和其它菜单动作一个口径）
        self._start_bg(self._reload_config_bg)

    def _reload_config_bg(self) -> None:
        self.state.reload()
        self._rebuild_menu()
        warnings = getattr(self.state, "load_warnings", [])
        if warnings:
            # JSON 被手改坏时不能只说"已生效"：把降级了什么（备份文件名）讲清楚
            self._flash("配置已重新加载（有降级）", "；".join(warnings))
        else:
            self._flash("已重新加载配置", "data/config.json 与 data/novels.json 已生效")

    def act_open_latest(self, novel_id: str, icon=None, item=None) -> None:
        novel = self.state.novel(novel_id)
        chapter = novel.latest_chapter() if novel else None
        if chapter is None:
            return
        if open_in_default_browser(chapter.url):
            self.state.mark_read(novel.id, chapter)
        else:
            self._flash("打开失败", chapter.url)

    def act_toggle_novel(self, novel_id: str, icon=None, item=None) -> None:
        novel = self.state.novel(novel_id)
        if novel is not None:
            self.state.set_novel_enabled(novel.id, not all(s.enabled for s in novel.sources))
            self._rebuild_menu()

    def act_delete_novel(self, novel_id: str, icon=None, item=None) -> None:
        """托盘里没有确认对话框，用「10 秒内点两次」代替。"""
        if self._armed(novel_id):
            novel = self.state.remove_novel(novel_id)
            self._pending_delete = None
            self.state.save_novels()
            self._rebuild_menu()
            self._flash("已删除", f"《{novel.display_title() if novel else novel_id}》移出书架，"
                                  f"data/history/{novel_id}/ 仍保留")
            return
        novel = self.state.novel(novel_id)
        self._arm(novel_id)
        self._flash("再点一次确认删除",
                    f"《{novel.display_title() if novel else novel_id}》：10 秒内再点一次「删除本书」")

    def act_set_primary(self, novel_id: str, source_id: str, icon=None, item=None) -> None:
        if self.state.set_primary(novel_id, source_id) is not None:
            self._rebuild_menu()

    def act_toggle_source(self, novel_id: str, source_id: str, icon=None, item=None) -> None:
        novel = self.state.novel(novel_id)
        source = novel.source(source_id) if novel else None
        if source is None:
            return
        self.state.set_source_enabled(novel_id, source_id, not source.enabled)
        self._rebuild_menu()

    def act_delete_source(self, novel_id: str, source_id: str, icon=None, item=None) -> None:
        """删来源同样点两次；只剩最后一个来源时等于把整本移出书架。"""
        key = f"{novel_id}#{source_id}"
        novel = self.state.novel(novel_id)
        site = novel.source(source_id).site if novel and novel.source(source_id) else source_id
        if self._armed(key):
            self.state.remove_source(novel_id, source_id)
            self.state.save_novels()
            self._pending_delete = None
            self._rebuild_menu()
            gone = self.state.novel(novel_id) is None
            self._flash("已移出来源", f"《{site}》{'，这本书没有来源了，已移出书架' if gone else ''}"
                        f"，data/history/ 里的记录保留")
            return
        self._arm(key)
        self._rebuild_menu()
        self._flash("再点一次确认删除来源", f"《{site}》：10 秒内再点一次「删除此源」")

    def _armed(self, key: str) -> bool:
        return bool(self._pending_delete and self._pending_delete[0] == key and
                    time.monotonic() - self._pending_delete[1] < _CONFIRM_WINDOW_SECONDS)

    def _arm(self, key: str) -> None:
        self._pending_delete = (key, time.monotonic())

    def act_set_interval(self, value: int | str, icon=None, item=None) -> None:
        self.state.set_poll_minutes(value)
        self._rebuild_menu()

    def act_toggle_autostart(self, icon=None, item=None) -> None:
        if autostart.is_enabled():
            autostart.disable()
            self._flash("已关闭开机自启", "")
        elif autostart.enable():
            self._flash("已开启开机自启", autostart.current_command())
        else:
            self._flash("开启失败", "无法写入注册表 Run 项")
        self._rebuild_menu()

    def act_open_data_dir(self, icon=None, item=None) -> None:
        try:
            os.startfile(str(self.state.paths.root))  # type: ignore[attr-defined]
        except OSError:
            self._flash("打不开数据目录", str(self.state.paths.root))

    def act_quit(self, icon=None, item=None) -> None:
        self.scheduler.stop()
        if self.icon is not None:
            self.icon.stop()

    # ---------- 界面刷新 ----------

    def _check_new_novel(self, novel_id: str) -> None:
        """新加的一本：先确认这个站直连还是走代理，再抓取；成功时卡片会说「已添加 + 最近 5 章」。"""
        probed = self._probe_or_report(novel_id)
        if probed is None:
            return
        # prefetch 必须按「来源 id」存（check_one 里按 source.id 取）：
        # 新书的首源 id 恰好等于书 id，但那是 add_novel 的巧合，不能依赖
        novel = self.state.novel(novel_id)
        source_id = novel.sources[0].id if novel and novel.sources else novel_id
        prefetch = {source_id: probed.html} if probed.html else None
        self._flash_outcomes(self.state.check_one(novel_id, prefetch=prefetch))
        self._rebuild_menu()

    def _check_new_source(self, novel_id: str, source_id: str) -> None:
        """新来源同样先探测代理；首轮只建档不弹卡（那批章节多半是旧章），这里给一句回执。"""
        probed = self._probe_or_report(source_id)
        if probed is None:
            return
        prefetch = {source_id: probed.html} if probed.html else None
        outcomes = self.state.check_one(novel_id, prefetch=prefetch)
        outcome = next((o for o in outcomes if o.source.id == source_id), None)
        if outcome is not None:
            self._flash(f"已挂上来源 {outcome.source.site}", outcome.detail())
        self._rebuild_menu()

    def _probe_or_report(self, source_id: str):
        """添加时自动确认该站走法：直连/代理都不通时弹指引卡并返回 None，调用方不再跑首轮。

        通了返回 ProbeResult：里面带着探测时抓回的首页 HTML，首轮建档直接复用。
        """
        result = self.state.ensure_source_proxy(source_id)
        if result is not None:
            return result
        self._flash(
            "连不上这个网站",
            "直连不通，也没试出可用的本机代理；可在 novels.json 给该来源手写 proxy，"
            "或删掉 config.json 里 site_proxies 的该站记录后重新添加一次重试",
        )
        self._rebuild_menu()
        return None

    def _flash_outcomes(self, outcomes) -> None:
        """弹了卡片的就不再回执；无更新、解析失败、代理不通这些得说一声。"""
        alerted = {alert.novel.id for alert in self.state.last_alerts}
        for outcome in outcomes:
            if outcome.novel.id in alerted or outcome.status in (STATUS_BASELINE, STATUS_UPDATED):
                continue
            self._flash(outcome.novel.display_title(), outcome.detail())

    def _after_round(self, outcomes) -> None:
        alerts = self._pending_alerts()
        failures = {o.source.id for o in outcomes if o.source.consecutive_failures}
        if alerts:
            self._status_hint = f"{len(alerts)} 本有新章节"
        elif failures:
            self._status_hint = f"{len(failures)} 个来源抓取失败"
        else:
            self._status_hint = "均无新章节"
        self._sync_icon_state()
        self._rebuild_menu()

    def _pending_alerts(self) -> list[NovelAlert]:
        """这一轮真正提醒过的本（不含刚建档的那张「已添加」卡片）。"""
        return [alert for alert in self.state.last_alerts if not alert.added]

    def _sync_icon_state(self) -> None:
        if self.icon is not None:
            self.icon.icon = make_icon_image(self._icon_state())

    def _icon_state(self) -> str:
        sources = self.state.sources
        if any(s.consecutive_failures >= self.state.cfg.failure_backoff_threshold for s in sources):
            return "error"
        if self._pending_alerts():
            return "update"
        return "normal"

    def _tooltip(self) -> str:
        novels = self.state.novels
        pending = len(self._pending_alerts())
        bits = [f"顺手 · 监控 {len(novels)} 本"]
        if pending:
            bits.append(f"{pending} 本待读")
        bits.append(self._status_hint)
        return " | ".join(bits)

    def _rebuild_menu(self) -> None:
        if self.icon is not None:
            self.icon.menu = self.build_menu()
            self.icon.title = self._tooltip()

    def _start_bg(self, fn, *args, **kwargs) -> None:
        """所有后台动作统一从这里起线程：线程里抛异常（写盘失败、站点解析崩了等）
        不能静默死掉，兜一张错误卡，现场才查得到。"""

        def runner() -> None:
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # 后台线程没有上层兜底，不截住整个线程就白死了
                try:
                    self._flash(f"操作失败：{getattr(fn, '__name__', '后台任务')}",
                                f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass  # 弹卡本身再失败也不能把异常扔回线程外

        threading.Thread(target=runner, daemon=True).start()

    def _flash(self, title: str, body: str) -> None:
        """托盘气泡在 Win11 已被砍掉，操作反馈统一走同一张卡片。"""
        self.notifier.show(title, [body] if body else [], "")

    # ---------- 菜单结构 ----------

    def build_menu(self) -> pystray.Menu:
        """pystray 的原生菜单只在 icon.menu 被重新赋值时重建，所以改完状态要 _rebuild_menu()。"""
        return pystray.Menu(
            pystray.MenuItem("小说列表", pystray.Menu(self._novel_items)),
            pystray.MenuItem("添加小说（从剪贴板）", self.act_add_from_clipboard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重新加载配置", self.act_reload_config),
            pystray.MenuItem("扫描间隔", pystray.Menu(self._interval_items)),
            pystray.MenuItem("开机自启", self.act_toggle_autostart,
                             checked=lambda item: autostart.is_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开数据目录", self.act_open_data_dir),
            pystray.MenuItem("退出", self.act_quit),
        )

    def _novel_items(self):
        novels = self.state.novels
        if not novels:
            yield pystray.MenuItem("（书架为空）", None, enabled=False)
            return
        for novel in novels:
            label = f"《{novel.display_title()}》 {self._novel_state_text(novel)}"
            yield pystray.MenuItem(label, pystray.Menu(partial(self._novel_submenu, novel.id)))

    def _novel_state_text(self, novel: Novel) -> str:
        states = {self._source_state_text(s) for s in novel.sources}
        if len(novel.sources) > 1:
            return f"{len(novel.sources)} 个来源 · {' / '.join(sorted(states))}"
        return next(iter(states), "")

    def _source_state_text(self, source: Source) -> str:
        if not source.enabled:
            return "已暂停"
        if source.status in (STATUS_UPDATED, STATUS_PARSE_FAILED, STATUS_PROXY_ERROR,
                             STATUS_CHECK_ERROR):
            return source.status_detail or status_label(source.status)
        return status_label(source.status)

    def _novel_submenu(self, novel_id: str):
        novel = self.state.novel(novel_id)
        if novel is None:
            return
        if novel.latest_chapter() is not None:
            yield pystray.MenuItem("打开最新章节", partial(self.act_open_latest, novel_id))
        yield pystray.MenuItem("添加来源到本书（从剪贴板）",
                               partial(self.act_add_source_from_clipboard, novel_id))
        yield pystray.MenuItem("暂停本书" if all(s.enabled for s in novel.sources) else "恢复本书",
                               partial(self.act_toggle_novel, novel_id))
        yield pystray.MenuItem("确认删除本书？" if self._armed(novel_id) else "删除本书",
                               partial(self.act_delete_novel, novel_id))
        yield pystray.Menu.SEPARATOR
        for source in novel.sources:
            hint = f"{source.site} · {self._source_state_text(source)}"
            if source.proxy.strip():
                # 代理是手改 json 配的，这里只读显示（不带账号密码），好确认改对了没有
                hint += f" · 代理 {proxy_label(source.proxy)}"
            label = f"来源 {hint}" + ("（主源）" if source.id == novel.primary_id else "")
            yield pystray.MenuItem(label, pystray.Menu(partial(self._source_submenu, novel_id, source.id)))

    def _source_submenu(self, novel_id: str, source_id: str):
        novel = self.state.novel(novel_id)
        source = novel.source(source_id) if novel else None
        if source is None:
            return
        yield pystray.MenuItem(source.toc_url, None, enabled=False)
        yield pystray.MenuItem(f"最后一章《{display_title(source.latest_title) or '—'}》 · 共 {source.chapter_count} 章",
                               None, enabled=False)
        yield pystray.MenuItem(self._proxy_state_text(source), None, enabled=False)
        if source.status == STATUS_PARSE_FAILED:
            yield pystray.MenuItem(f"手写 selector → data/candidates/{source.id}.txt",
                                   None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("设为主源", partial(self.act_set_primary, novel_id, source_id),
                               enabled=novel.primary_id != source.id)
        yield pystray.MenuItem("暂停此源" if source.enabled else "恢复此源",
                               partial(self.act_toggle_source, novel_id, source_id))
        yield pystray.MenuItem("确认删除此源？" if self._armed(f"{novel_id}#{source_id}") else "删除此源",
                               partial(self.act_delete_source, novel_id, source_id))

    @staticmethod
    def _proxy_state_text(source: Source) -> str:
        """代理状态如实显示：没配就是直连（程序已禁用系统代理，环境变量/注册表都不走）。"""
        if source.proxy.strip():
            return f"代理 {proxy_label(source.proxy)}（手写覆盖）"
        return "代理 直连（不走系统代理）"

    def _interval_items(self):
        """托盘菜单「扫描间隔」子菜单：固定四个选项，用户四选一。"""
        current = self.state.cfg.poll_minutes
        for value in POLL_MINUTES:
            yield pystray.MenuItem(
                _INTERVAL_LABELS[value],
                partial(self.act_set_interval, value),
                checked=partial(self._interval_checked, value, current),
                radio=True,
            )

    @staticmethod
    def _interval_checked(value, current, item=None) -> bool:
        return current == value

    # ---------- 运行 ----------

    def run(self) -> None:
        if not acquire_singleton():
            self._flash("顺手已经在运行", "托盘里已有一个实例，这次不再重复启动")
            return
        autostart.repair_if_drifted()
        self.icon = pystray.Icon("shunshu", title=self._tooltip(), icon=make_icon_image("normal"),
                                 menu=self.build_menu())
        threading.Thread(target=self._startup_then_loop, name="shunshu-poll", daemon=True).start()
        self.icon.run()

    def _startup_then_loop(self) -> None:
        try:
            outcomes = self.scheduler.run_once(force=True)
        except Exception as exc:
            outcomes = []
            self._flash("启动检查失败", repr(exc))
        lines = self.state.startup_lines(outcomes)
        self._status_hint = lines[0] if lines else self._status_hint
        self._sync_icon_state()
        self._rebuild_menu()
        body = [line for line in lines[1:1 + self.state.cfg.history_display_cap]]
        # 启动时配置/书架发生过降级（坏 JSON 已备份）：把原因顶到启动卡最前面
        body = [*getattr(self.state, "load_warnings", []), *body]
        launch = next((o.latest.url for o in outcomes if o.latest), "")
        self.notifier.show("顺手 · 启动检查", body or ["书架为空"], launch)
        self.scheduler.run_forever()
