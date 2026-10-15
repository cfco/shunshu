"""轮询循环：固定 5/10/30/60 分钟间隔，失败的书按指数退避跳过。

退避逻辑按来源算：连续失败次数 ≥ failure_backoff_threshold 之后，
下一轮被跳过的时间 = interval × 2^(failure_count - threshold + 1)，封顶 8 倍间隔。

暂停时段（config 的 pause_start~pause_end，默认对齐午休）内的那一轮不敲站点，直接睡到
时段结束再跑：期间更新的章节由出时段后的第一轮一起报出来，卡片落在人回到工位的时候。
"""

from __future__ import annotations

import threading
import time

from .app import AppState


class Scheduler:
    def __init__(self, state: AppState, on_round=None, on_pause=None):
        self.state = state
        self.on_round = on_round
        # 进入暂停时段时叫一声（托盘用它把状态提示改成「暂停中，X 分钟后恢复」）
        self.on_pause = on_pause
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._busy = threading.Lock()
        # 每个来源的下一轮最早可检查时间戳（秒），用于退避
        self._next_due: dict[str, float] = {}
        self.last_round_at: float | None = None
        # 同一段暂停只在跨进时段的那一次记日志、叫一次托盘
        self._pause_announced = False

    def next_wait_seconds(self) -> float:
        """返回下一轮的等待秒数：固定间隔，不做抖动。"""
        return max(30.0, float(self.state.cfg.poll_minutes) * 60.0)

    def due_skip_ids(self, now: float) -> set[str]:
        """返回所有还没到退避结束时间、本轮应该跳过的来源 id。"""
        return {sid for sid, due in self._next_due.items() if due > now}

    def after_round(self, outcomes, skipped: set[str] | None = None) -> None:
        """根据本轮结果更新每个来源的退避时间表。

        本轮被退避跳过的来源不重算 next_due：跳过的那一轮根本没抓取，consecutive_failures
        不变，若每轮都把 next_due 推到 now + factor*interval（factor≥2），due 退得比时间
        走得还快，来源就永远轮不到重试——连续失败到阈值的站会在本次进程里被永久跳过。
        保持原 due 不动，到点自然解冻重试；真被重试且又失败时，下面的分支会按涨上来的
        失败次数重算一个更长的退避。
        """
        skipped = skipped or set()
        interval = self.next_wait_seconds()
        threshold = self.state.cfg.failure_backoff_threshold
        now = time.time()
        for outcome in outcomes:
            source = outcome.source
            if source.id in skipped:
                continue
            if source.consecutive_failures >= threshold:
                # 连续失败越多，跳过越长；封顶 8 倍间隔，避免永远跳不过去
                factor = min(2 ** (source.consecutive_failures - threshold + 1), 8)
                self._next_due[source.id] = now + interval * factor
            else:
                # 成功或失败次数还没到阈值，清掉退避
                self._next_due.pop(source.id, None)

    def run_once(self, *, force: bool = False) -> list:
        """跑一轮：force=True 时忽略退避直接检查全部来源。"""
        with self._busy:
            skip = set() if force else self.due_skip_ids(time.time())
            outcomes = self.state.run_round(skip_ids=skip)
            self.after_round(outcomes, skipped=skip)
            self.last_round_at = time.time()
        if self.on_round:
            self.on_round(outcomes)
        return outcomes

    def run_forever(self) -> None:
        """主循环：按固定间隔一轮一轮跑，落在暂停时段里的那一轮顺延到时段结束。

        启动那一轮由调用方（托盘）显式跑过并拿结果弹启动卡片，这里不能再补一轮，
        否则每次开机都把书架里所有站点连敲两遍。暂停顺延只管这里，手动动作不拦：
        点开托盘、添加书源那一下本来人就盯着屏幕。
        """
        while not self._stop.is_set():
            left = self.state.cfg.pause_seconds_left()
            if left is None:
                wait = self.next_wait_seconds()
                self._pause_announced = False
            else:
                wait = left
                self._announce_pause(left)
            # _wake 用来打断当前这一觉：退出时立刻收尾，托盘关掉暂停时也提前恢复检查
            if self._wake.wait(timeout=wait):
                self._wake.clear()
            if self._stop.is_set():
                break
            if self.state.cfg.pause_seconds_left() is not None:
                # 醒来还在时段里：睡的过程中时段被改长，或系统时钟被调过
                continue
            try:
                self.run_once()
            except Exception as exc:  # 单轮异常不能让循环停掉
                from .store import append_text, now_stamp

                append_text(self.state.paths.runtime_log, f"{now_stamp()} 轮询异常: {exc!r}")

    def _announce_pause(self, left: float) -> None:
        """跨进暂停时段时留一句证据：日志一行 + 托盘状态提示，别让人以为程序悄悄停了。

        这段只是附赠信息，却在主循环的 try 之外被调用：写盘失败或托盘回调炸了要是冒出去，
        轮询线程会当场死掉——托盘还在、日志不再写、从此再不检查且没有任何提示
        （同一个坑见 config._coerce_field 的注释）。所以这里自己兜住。
        """
        if self._pause_announced:
            return
        self._pause_announced = True
        cfg = self.state.cfg
        # 给托盘的那句不带「还剩几分钟」：悬浮提示会一直挂在鼠标上，倒计时放日志里（带时间戳，
        # 说的是写下那一行的当时），提示只说稳定的「什么时候恢复」
        text = f"暂停时段 {cfg.pause_start}~{cfg.pause_end} 内不抓取，{cfg.pause_end} 恢复检查"
        try:
            from .store import append_text, now_stamp

            append_text(self.state.paths.runtime_log,
                        f"{now_stamp()} {text}（本轮顺延，约 {round(left / 60)} 分钟后）")
            if self.on_pause:
                self.on_pause(text)
        except Exception:  # 提示没送出去也照样顺延，出时段的第一轮会自己把状态刷回来
            pass

    def resume_now(self) -> None:
        """结束当前这一觉，让刚改过的设置立刻生效。

        用在「时段正中间关掉暂停开关」：那一觉是按「距时段结束还剩多久」睡的，
        不叫醒就得干等到点才恢复检查，用户已经允许抓取了却两小时没动静。
        """
        self._wake.set()

    def stop(self) -> None:
        """请求循环退出。"""
        self._stop.set()
        self._wake.set()
