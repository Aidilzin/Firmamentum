"""Typed run events. Every event carries a deep-frozen snapshot of the run's meta, so a consumer on another thread
can never see (or break) the executor's own dict.

This module imports nothing from the executor, the views or the ui. Terminal rule: every public executor call that got
past its preconditions ends with exactly one of `Stopped`, `RunFinished` or `RunCrashed`, and it is the last event.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

Meta = Mapping[str, Any]


def freeze(x: Any) -> Any:
    """dict -> MappingProxyType, list/tuple -> tuple, set -> frozenset, recursively. Scalars pass through."""
    if isinstance(x, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in x.items()})
    if isinstance(x, (list, tuple)):
        return tuple(freeze(v) for v in x)
    if isinstance(x, (set, frozenset)):
        return frozenset(freeze(v) for v in x)
    return x


def thaw(x: Any) -> Any:
    """The reverse of `freeze`: plain dicts and lists, for `json.dumps` and anything that needs to mutate."""
    if isinstance(x, Mapping):
        return {k: thaw(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [thaw(v) for v in x]
    if isinstance(x, (set, frozenset)):
        return {thaw(v) for v in x}
    return x


@dataclass(frozen=True, slots=True)
class RunEvent:
    seq: int  # per executor, strictly increasing
    run_id: str
    meta: Meta  # the whole meta, frozen, as saved when the event was emitted


@dataclass(frozen=True, slots=True)
class RunStarted(RunEvent):
    action: str  # start | resume | approve | answer


@dataclass(frozen=True, slots=True)
class StepStarted(RunEvent):
    step_id: str
    title: str
    phase: str | None  # "build" = a fresh attempt; anything else = continuing at that stored phase


@dataclass(frozen=True, slots=True)
class PhaseChanged(RunEvent):
    step_id: str
    phase: str  # exactly the stored phase, so need_repair and repair are both seen


@dataclass(frozen=True, slots=True)
class AgentCalled(RunEvent):
    step_id: str
    role: str
    entry: str  # label of the entry that served the call (after any fallback hop); the agent name after a rate-limit pause
    fallback: bool


@dataclass(frozen=True, slots=True)
class AgentFinished(RunEvent):
    step_id: str
    role: str
    tokens: int
    ok: bool
    error_kind: str | None  # ErrorKind value, None when ok


@dataclass(frozen=True, slots=True)
class StepFinished(RunEvent):
    step_id: str
    commit: str | None


@dataclass(frozen=True, slots=True)
class Stopped(RunEvent):
    outcome: str  # paused | cancelled | failed | needs_input | awaiting_approval | rejected
    step_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class RunFinished(RunEvent):
    message: str


@dataclass(frozen=True, slots=True)
class RunCrashed(RunEvent):
    """An unexpected exception left the run. An event only, not an ExecOutcome; meta is the last saved state."""

    error: str


TERMINAL = (Stopped, RunFinished, RunCrashed)
DROPPABLE = (PhaseChanged, AgentCalled)  # the only events a full buffer may discard (the shell prints lines from the rest)


class EventBuffer:
    """A thread-safe, bounded hand-off from the executor thread to a UI thread. `emit` never blocks.

    When full, the oldest PhaseChanged/AgentCalled is dropped first; any other event is dropped only when the buffer
    holds nothing droppable. Each event carries a full snapshot, so state-based views stay correct after a drop."""

    def __init__(self, maxlen: int = 500):
        self.maxlen = maxlen
        self.dropped = 0
        self._items: deque[RunEvent] = deque()
        self._cond = threading.Condition()

    def emit(self, ev: RunEvent) -> None:
        with self._cond:
            if len(self._items) >= self.maxlen:
                oldest = next((i for i, e in enumerate(self._items) if isinstance(e, DROPPABLE)), None)
                if oldest is not None:
                    del self._items[oldest]
                elif isinstance(ev, DROPPABLE):
                    self.dropped += 1  # nothing droppable is queued, so the newcomer is the one to lose
                    return
                else:
                    self._items.popleft()  # only protected events are left: the oldest one goes
                self.dropped += 1
            self._items.append(ev)
            self._cond.notify_all()

    def drain(self, timeout: float | None = 0.0) -> list[RunEvent]:
        """Everything queued, oldest first. Waits up to `timeout` seconds when empty (None: forever)."""
        with self._cond:
            if not self._items and timeout != 0.0:
                self._cond.wait(timeout)
            out = list(self._items)
            self._items.clear()
            return out

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)
