"""Bounded command telemetry; never executes commands or grants permissions."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Iterator, Optional


@dataclass
class CommandTiming:
    duration_seconds: float = 0.0


@contextmanager
def observe_command(
    on_wait: Optional[Callable[[float], None]] = None,
    *,
    interval_seconds: float = 15.0,
) -> Iterator[CommandTiming]:
    """Report elapsed time without exposing output or altering command results.

    The caller still owns argv, deadline, execution, redaction and exit codes.
    Callbacks must be non-blocking. A broken observer cannot fail a quality gate.
    """
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("Heartbeat interval must be finite and positive")
    timing = CommandTiming()
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(interval_seconds):
            try:
                on_wait(round(time.monotonic() - started, 3))
            except Exception:
                # Diagnostics are never authority for PASS, FAIL, or retries.
                pass

    worker = None
    if on_wait is not None:
        worker = threading.Thread(target=heartbeat, name="yy-flow-command-heartbeat", daemon=True)
        worker.start()
    try:
        yield timing
    finally:
        timing.duration_seconds = round(max(0.0, time.monotonic() - started), 3)
        stopped.set()
        if worker is not None:
            worker.join(timeout=1.0)
