"""One executor per run: an OS lock on `<run>/lock`, held for the life of the executor.

Liveness is decided by the OS lock, never by the stored pid or heartbeat: the OS drops the lock when the process dies for
any reason, and a pid can be reused. The pid and heartbeat are stored only so a refusal can say who holds the run.
The lock sits on one byte far beyond the info block, so other processes can still read the info while it is held.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

LOCK_NAME = "lock"
LOCK_OFFSET = 1 << 20  # the locked byte; the info block lives at offset 0
INFO_SIZE = 512  # fixed size, so rewriting never truncates
BEAT_EVERY_S = 5.0
ACQUIRE_RETRY_S = 0.3  # a `holder()` probe from another terminal holds the lock for an instant


@dataclass
class LockInfo:
    pid: int | None = None
    started_at: str = ""
    heartbeat: str = ""

    def describe(self) -> str:
        who = f"pid {self.pid}" if self.pid else "another process"
        return f"{who}" + (f" since {self.started_at}" if self.started_at else "")


class RunLocked(Exception):
    def __init__(self, info: LockInfo):
        super().__init__(f"run is locked by {info.describe()}")
        self.info = info


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _try_lock(fd: int) -> bool:
    try:
        if IS_WINDOWS:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if IS_WINDOWS:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def read_info(path: Path) -> LockInfo:
    try:
        raw = path.read_bytes()[:INFO_SIZE].decode("utf-8", errors="replace").strip(" \x00\n")
        data = json.loads(raw)
        return LockInfo(int(data.get("pid") or 0) or None, str(data.get("started_at", "")), str(data.get("heartbeat", "")))
    except (OSError, ValueError, TypeError):
        return LockInfo()


class RunLock:
    def __init__(self, fd: int, path: Path, started_at: str):
        self._fd, self.path, self._started = fd, path, started_at
        self._last_beat = 0.0

    def _write(self) -> None:
        text = json.dumps({"pid": os.getpid(), "started_at": self._started, "heartbeat": _now()})
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, text.encode("utf-8").ljust(INFO_SIZE, b" "))
        self._last_beat = time.monotonic()

    def beat(self) -> None:
        """Refresh the heartbeat, at most every few seconds."""
        if self._fd >= 0 and time.monotonic() - self._last_beat >= BEAT_EVERY_S:
            try:
                self._write()
            except OSError:
                pass  # the heartbeat is informational; never fail a save over it

    def release(self) -> None:
        if self._fd < 0:
            return
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = -1


def lock_path(run_dir: Path) -> Path:
    return Path(run_dir) / LOCK_NAME


def acquire(run_dir: Path) -> RunLock:
    """Take the run's lock or raise RunLocked (after a short retry)."""
    path = lock_path(run_dir)
    deadline = time.monotonic() + ACQUIRE_RETRY_S
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        if _try_lock(fd):
            lock = RunLock(fd, path, _now())
            lock._write()
            return lock
        os.close(fd)
        if time.monotonic() >= deadline:
            raise RunLocked(read_info(path))
        time.sleep(0.05)


def holder(run_dir: Path) -> LockInfo | None:
    """Who holds the run right now, or None when nobody does (including: it never had a lock). Creates no file."""
    path = lock_path(run_dir)
    if not path.is_file():
        return None
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return LockInfo()  # cannot even open it: treat as held rather than call a live run dead
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
    finally:
        os.close(fd)
    return read_info(path)
