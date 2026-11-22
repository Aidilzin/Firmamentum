"""A stand-in for the Docker engine. Tests never start Docker; `conftest.fake_docker` installs one of these.

`docker_run` runs the command on the host (in the folder that would have been mounted) and keeps a record of the
container, the way the engine does. A cancelled or timed-out run leaves its container "running": killing the docker CLI
does not stop the container, only `docker rm -f` does. `docker_call` answers `ps`, `rm` and `inspect` from that record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from orch.adapters.process import ProcessOutput, child_env, run_process


@dataclass
class Box:
    id: str
    name: str
    label: str
    status: str = "running"  # running | exited | created
    exit_code: int = 0
    oom: bool = False
    error: str = ""


def _after(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


class DockerSim:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []  # every argv sent to the engine, in order (run and short calls)
        self.runs: list[dict] = []  # one entry per docker_run: argv, inner, src, env
        self.boxes: dict[str, Box] = {}  # by name
        self.script: list[Callable[[DockerSim, Box, list[str]], ProcessOutput]] = []  # overrides, one per docker_run
        self.fail: dict[str, ProcessOutput] = {}  # a short call that fails, by subcommand ("ps", "rm", "inspect")
        self.on_call: Callable[[list[str]], None] | None = None  # runs before every call (to plant or remove things)
        self._n = 0

    # -- helpers for tests --
    def plant(self, label: str, status: str = "running", name: str | None = None) -> Box:
        self._n += 1
        box = Box(f"{self._n:012x}", name or f"orch-{label}-planted{self._n}", label, status)
        self.boxes[box.name] = box
        return box

    def running(self, label: str | None = None) -> list[str]:
        return [b.name for b in self.boxes.values() if b.status == "running" and label in (None, b.label)]

    def verbs(self) -> list[str]:
        """`run`, `ps`, `rm`, `inspect` ... in call order."""
        return [c[1] for c in self.calls if len(c) > 1]

    def labels_run(self) -> list[str]:
        return [a[a.index("--label") + 1] for a in (r["argv"] for r in self.runs)]

    # -- the two seams --
    def docker_run(self, argv, *, inner, src, env, timeout_s, cancel=None) -> ProcessOutput:
        self.calls.append(list(argv))
        self.runs.append({"argv": list(argv), "inner": list(inner), "src": src, "env": env})
        if self.on_call:
            self.on_call(argv)
        label = (_after(argv, "--label") or "").removeprefix("orch.run=")
        name = _after(argv, "--name") or f"anon-{len(self.runs)}"
        self._n += 1
        box = self.boxes[name] = Box(f"{self._n:012x}", name, label)
        if self.script:
            return self.script.pop(0)(self, box, argv)
        out = run_process(inner, cwd=src, env=env or child_env(), timeout_s=timeout_s, cancel=cancel)
        if out.cancelled or out.timed_out:
            return out  # the docker CLI was killed; the container is still "running" until someone removes it
        if out.not_found:  # the entry script's `exec` failed
            box.status, box.exit_code = "exited", 127
            return ProcessOutput(stderr=out.error, returncode=127)
        box.status, box.exit_code = "exited", out.returncode if out.returncode is not None else 1
        return out

    def docker_call(self, argv, timeout_s: int = 15) -> ProcessOutput:
        self.calls.append(list(argv))
        if self.on_call:
            self.on_call(argv)
        verb = argv[1] if len(argv) > 1 else ""
        if verb in self.fail:
            return self.fail[verb]
        if verb == "ps":
            flt = _after(argv, "--filter") or ""
            want_all = any(a in ("-a", "-aq") for a in argv)
            if flt.startswith("name="):  # substring match, like the engine
                pick = lambda b: flt.removeprefix("name=") in b.name  # noqa: E731
            else:
                label = flt.removeprefix("label=")
                pick = lambda b: b.label == label.removeprefix("orch.run=") if "=" in label else True  # noqa: E731
            hits = [b for b in self.boxes.values() if (want_all or b.status == "running") and pick(b)]
            if "--format" in argv:
                return ProcessOutput(stdout="".join(f"{b.id}|{b.label}|{_status_text(b)}\n" for b in hits), returncode=0)
            return ProcessOutput(stdout="".join(b.id + "\n" for b in hits), returncode=0)
        if verb == "rm":
            ids = [a for a in argv[2:] if not a.startswith("-")]
            for name in [n for n, b in self.boxes.items() if b.id in ids]:
                del self.boxes[name]
            return ProcessOutput(stdout="\n".join(ids), returncode=0)
        if verb == "inspect":
            box = self.boxes.get(argv[-1])
            if box is None:
                return ProcessOutput(stderr=f"Error: No such object: {argv[-1]}", returncode=1)
            oom = "true" if box.oom else "false"
            return ProcessOutput(stdout=f"{box.status}|{box.exit_code}|{oom}|{box.error}\n", returncode=0)
        return ProcessOutput(stderr=f"fake engine: unsupported call {argv}", returncode=1)


def _status_text(b: Box) -> str:
    return "Up 3 minutes" if b.status == "running" else f"Exited ({b.exit_code}) 2 minutes ago"
