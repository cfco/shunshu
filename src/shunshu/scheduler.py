"""轮询循环：固定 5/10/30/60 分钟间隔，失败的书按指数退避跳过。

退避逻辑按来源算：连续失败次数 ≥ failure_backoff_threshold 之后，
下一轮被跳过的时间 = interval × 2^(failure_count - threshold + 1)，封顶 8 倍间隔。
"""

from __future__ import annotations

import threading
import time

from .app import AppState


class Scheduler:
    def __init__(self, state: AppState, on_round=None):
        self.state = state
        self.on_round = on_round
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._busy = threading.Lock()
        # 每个来源的下一轮最早可检查时间戳（秒），用于退避
        self._next_due: dict[str, float] = {}
        self.last_round_at: float | None = None

    def next_wait_seconds(self) -> float:
        """返回下一轮的等待秒数：固定间隔，不做抖动。"""
        return max(30.0, float(self.state.cfg.poll_minutes) * 60.0)

    def due_skip_ids(self, now: float) -> set[str]:
        """返回所有还没到退避结束时间、本轮应该跳过的来源 id。"""
        return {sid for sid, due in self._next_due.items() if due > now}

    def after_round(self, outcomes) -> None:
        """根据本轮结果更新每个来源的退避时间表。"""
        interval = self.next_wait_seconds()
        threshold = self.state.cfg.failure_backoff_threshold
        now = time.time()
        for outcome in outcomes:
            source = outcome.source
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
            self.after_round(outcomes)
            self.last_round_at = time.time()
        if self.on_round:
            self.on_round(outcomes)
        return outcomes

    def run_forever(self) -> None:
        """主循环：按固定间隔一轮一轮跑。

        启动那一轮由调用方（托盘）显式跑过并拿结果弹启动卡片，这里不能再补一轮，
        否则每次开机都把书架里所有站点连敲两遍。
        """
        while not self._stop.is_set():
            wait = self.next_wait_seconds()
            # _wake 只用于 stop() 立刻打断等待，让退出不用再等一个完整间隔
            if self._wake.wait(timeout=wait):
                self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.run_once()
            except Exception as exc:  # 单轮异常不能让循环停掉
                from .store import append_text, now_stamp

                append_text(self.state.paths.runtime_log, f"{now_stamp()} 轮询异常: {exc!r}")

    def stop(self) -> None:
        """请求循环退出。"""
        self._stop.set()
        self._wake.set()
