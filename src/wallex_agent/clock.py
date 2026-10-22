"""Clock abstraction so backtests/tests run in simulated time."""
from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Epoch seconds."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class SimClock:
    """Deterministic clock: ``sleep`` advances time instantly."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += float(seconds)

    def set(self, t: float) -> None:
        with self._lock:
            self._t = float(t)
