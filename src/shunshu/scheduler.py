"""轮询循环：固定 3/5/10 分钟或随机 3~10 分钟，失败的书按指数退避跳过。"""

from __future__ import annotations

import random
import threading
import time

from .app import AppState
from .config import POLL_RANDOM


class Scheduler:
    def __init__(self, state: AppState, on_round=None, rng: random.Random | None = None):
        self.state = state
        self.on_round = on_round
        self.rng = rng or random.Random()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._busy = threading.Lock()
        self._next_due: dict[str, float] = {}
        self.paused_until = 0.0
        self.last_round_at: float | None = None

    def next_wait_seconds(self) -> float:
        cfg = self.state.cfg
        minutes = cfg.poll_minutes
        if minutes == POLL_RANDOM:
            low, high = (list(cfg.random_range_minutes) + [3, 10])[:2]
            return max(30.0, self.rng.uniform(float(low), float(high)) * 60.0)
        base = float(minutes) * 60.0
        return max(30.0, base * (1.0 + self.rng.uniform(-cfg.jitter_ratio, cfg.jitter_ratio)))

    def due_skip_ids(self, now: float) -> set[str]:
        return {sid for sid, due in self._next_due.items() if due > now}

    def after_round(self, outcomes) -> None:
        """连续失败越多，下一轮被跳过的时间越长：退避倍数封顶 8 倍间隔。退避按来源算。"""
        interval = self.next_wait_seconds()
        threshold = self.state.cfg.failure_backoff_threshold
        now = time.time()
        for outcome in outcomes:
            source = outcome.source
            if source.consecutive_failures >= threshold:
                factor = min(2 ** (source.consecutive_failures - threshold + 1), 8)
                self._next_due[source.id] = now + interval * factor
            else:
                self._next_due.pop(source.id, None)

    def run_once(self, *, force: bool = False) -> list:
        with self._busy:
            skip = set() if force else self.due_skip_ids(time.time())
            outcomes = self.state.run_round(skip_ids=skip)
            self.after_round(outcomes)
            self.last_round_at = time.time()
        if self.on_round:
            self.on_round(outcomes)
        return outcomes

    def run_forever(self) -> None:
        self.run_once(force=True)
        while not self._stop.is_set():
            now = time.time()
            if self.paused_until > now:
                wait = max(1.0, self.paused_until - now)
            else:
                wait = self.next_wait_seconds()
            if self._wake.wait(timeout=wait):
                self._wake.clear()
            if self._stop.is_set():
                break
            if self.paused_until > time.time():
                continue
            try:
                self.run_once()
            except Exception as exc:  # 单轮异常不能让循环停掉
                from .store import append_text, now_stamp

                append_text(self.state.paths.runtime_log, f"{now_stamp()} 轮询异常: {exc!r}")

    def trigger_now(self) -> None:
        """手动「立即检查」：清掉退避时间并唤醒循环。"""
        self._next_due.clear()
        self._wake.set()

    def pause(self, minutes: float | None) -> None:
        if minutes is None:
            self.paused_until = time.time() + 365 * 24 * 3600
        else:
            self.paused_until = time.time() + minutes * 60

    def resume(self) -> None:
        self.paused_until = 0.0

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
