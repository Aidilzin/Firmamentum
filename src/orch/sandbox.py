"""Docker sandbox for agent-written code (docs/sandbox-design.md). Standard library only.

Everything the model can make run (the test command, conftest.py, ...) goes into a container with no network, a read-only
copy of the source, no host environment and no git metadata. The functions that build argv lists are pure, so the tests
check the exact flags without Docker. Only `docker_probe` talks to the engine, through the shared `run_process`.

Fail closed: for a repo whose sandbox is `docker`, `require` raises when the engine or the image is missing. There is no
fallback to running on the host; the only way out is an explicit `sandbox: off` for that repo in config.yaml.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Literal

from .adapters.process import ProcessOutput, child_env, run_process
from .config import ResolvedRepo, SandboxConfig

CACHE_OK_S = 30.0  # a successful probe may be reused this long (display only); `require` never uses it
PROBE_TIMEOUT_S = 10  # `docker info` hangs while Docker Desktop is half started; do not wait longer than this
UV_CACHE_VOLUME = "orch-uv-cache"
RUN_LABEL = "orch.run"
SANDBOX_USER = "1000:1000"
OFF_WARNING = "sandbox: off (tests run on this machine, unconfined)"

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
_VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTAINER_ID = re.compile(r"[0-9a-f]{6,64}")
_BAD_MOUNT_CHARS = set(',"\'\n\r\0')  # `--mount` is a comma separated list; these would let a path add options


class SandboxError(Exception):
    """A sandbox command cannot be built (bad path, bad name)."""


class SandboxUnavailable(SandboxError):
    """Docker is not running or the image is missing. The message tells the human what to do."""


@dataclass(frozen=True)
class Spec:
    image: str
    memory: str = "4g"
    cpus: int = 4

    @classmethod
    def for_repo(cls, repo: ResolvedRepo, cfg: SandboxConfig) -> Spec:
        return cls(image=repo.image, memory=cfg.memory, cpus=cfg.cpus)


# ---------- pure argv builders ----------


def _src_arg(src: Path) -> str:
    s = str(src)
    if not Path(src).is_absolute():
        raise SandboxError(f"sandbox source must be an absolute path, got {s!r}")
    if _BAD_MOUNT_CHARS & set(s):
        raise SandboxError(f"sandbox source path has a character docker's --mount cannot take safely: {s!r}")
    return s


def _check(value: str, rx: re.Pattern[str], what: str) -> str:
    if not rx.fullmatch(value or ""):
        raise SandboxError(f"bad {what}: {value!r}")
    return value


def _passthrough(names: tuple[str, ...] | list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        _check(n, _ENV_NAME, "environment variable name")
        if n.upper().startswith("ANTHROPIC_"):
            continue  # never reaches any child, same rule as child_env
        out += ["-e", n]  # name only: docker takes the value from its own environment, so it is not in the argv
    return out


def container_name(run_id: str) -> str:
    """`orch-<run id>-<8 hex>`: unique per container, so `docker inspect` can ask about exactly this one."""
    return f"orch-{_check(run_id, _RUN_ID, 'run id')}-{secrets.token_hex(4)}"


def _hardening(spec: Spec, run_id: str, name: str) -> list[str]:
    # No `--rm`: the stopped container must still exist after the run so `docker inspect` can say why it stopped
    # (exit code, out of memory, never started). `sweep` removes it afterwards.
    return [
        "--name", _check(name, _VOLUME, "container name"), "--label", f"{RUN_LABEL}={_check(run_id, _RUN_ID, 'run id')}",
        "--user", SANDBOX_USER, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp", "--tmpfs", f"/work:exec,uid={SANDBOX_USER.split(':')[0]},size=2g",
        "--pids-limit", "512", "--memory", spec.memory, "--cpus", str(spec.cpus),
    ]  # fmt: skip


def test_argv(
    spec: Spec, src: Path, command: list[str], run_id: str, venv_volume: str, passthrough: tuple[str, ...] | list[str] = (),
    *, name: str | None = None,
) -> list[str]:  # fmt: skip
    """`docker run` for one offline test run: no network, source read-only at /src (copied to /work by the image's entry
    script), the prepared venv read-only at /venv. `command` comes from config.yaml, never from a plan."""
    if not command or not all(isinstance(c, str) and c for c in command):
        raise SandboxError("sandbox command must be a non-empty list of strings")
    return [
        "docker", "run", *_hardening(spec, run_id, name if name is not None else container_name(run_id)), "--network", "none",
        "--mount", f"type=bind,src={_src_arg(src)},dst=/src,readonly",
        "--mount", f"type=volume,src={_check(venv_volume, _VOLUME, 'volume name')},dst=/venv,readonly",
        "-e", "UV_PROJECT_ENVIRONMENT=/venv", "-e", "UV_NO_SYNC=1", "-e", "UV_OFFLINE=1", "-e", "UV_PYTHON_DOWNLOADS=never",
        "-e", "UV_CACHE_DIR=/tmp/uvc",
        *_passthrough(passthrough),
        "-w", "/work", spec.image, *command,
    ]  # fmt: skip


test_argv.__test__ = False  # not a pytest test, whatever module imports it


# The marker is written last, so a warm that was killed half way is never taken for a finished one (entry.sh checks it).
WARM_COMMAND = ["sh", "-c", "uv sync --frozen && touch /venv/.orch-ready"]
WARM_TIMEOUT_S = 600


def warm_argv(spec: Spec, src: Path, run_id: str, venv_volume: str, *, name: str | None = None) -> list[str]:
    """`docker run` for the one step that has network: `uv sync --frozen` into the venv volume, on content you committed or
    approved. The uv cache is a named volume, so a new venv is filled from it without downloading again."""
    return [
        "docker", "run", *_hardening(spec, run_id, name if name is not None else container_name(run_id)),
        "--mount", f"type=bind,src={_src_arg(src)},dst=/src,readonly",
        "--mount", f"type=volume,src={_check(venv_volume, _VOLUME, 'volume name')},dst=/venv",
        "--mount", f"type=volume,src={UV_CACHE_VOLUME},dst=/uvcache",
        "-e", "UV_PROJECT_ENVIRONMENT=/venv", "-e", "UV_CACHE_DIR=/uvcache", "-e", "UV_LINK_MODE=copy",
        "-w", "/work", spec.image, *WARM_COMMAND,
    ]  # fmt: skip


def venv_volume_name(src: Path, image_id: str) -> str:
    """`orch-venv-<12 hex>`: changes when pyproject.toml, uv.lock or the image changes, so an unchanged project reuses it."""
    h = hashlib.sha256()
    for part in (Path(src, "pyproject.toml"), Path(src, "uv.lock")):
        data = part.read_bytes() if part.is_file() else b""
        h.update(len(data).to_bytes(8, "big") + data)
    h.update(image_id.encode("utf-8"))
    return "orch-venv-" + h.hexdigest()[:12]


def dockerfile_dir() -> Path:
    """The `sandbox/` folder next to src/ (Dockerfile and entry.sh), the same way config.yaml is found."""
    return Path(__file__).resolve().parents[2] / "sandbox"


def build_argv(image: str, context: Path) -> list[str]:
    """`docker build` for the sandbox image. The image name is checked so it can never be read as a docker flag."""
    return ["docker", "build", "--tag", _check(image, _IMAGE_NAME, "image name"), str(context)]


# ---------- talking to the engine ----------


@dataclass(frozen=True)
class ProbeResult:
    engine_ok: bool
    image_ok: bool = False
    image_id: str = ""
    server: str = ""
    detail: str = ""  # why it is not ok, one line

    @property
    def ok(self) -> bool:
        return self.engine_ok and self.image_ok


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")[:200]


def docker_probe(image: str) -> ProbeResult:
    """Ask the engine two questions (`docker info`, `docker image inspect`). Makes no change and no model call."""
    env = child_env()
    info = run_process(["docker", "info", "--format", "{{.ServerVersion}}"], cwd=Path.cwd(), env=env, timeout_s=PROBE_TIMEOUT_S)
    if info.not_found:
        return ProbeResult(False, detail="the docker command was not found on PATH")
    if info.timed_out:
        return ProbeResult(False, detail=f"docker did not answer within {PROBE_TIMEOUT_S} s")
    server = info.stdout.strip()
    if info.returncode != 0 or not server:
        return ProbeResult(False, detail=_first_line(info.stderr) or info.error or "docker info failed")
    ins = run_process(["docker", "image", "inspect", "--format", "{{.Id}}", image], cwd=Path.cwd(), env=env,
                      timeout_s=PROBE_TIMEOUT_S)  # fmt: skip
    if ins.timed_out:
        return ProbeResult(True, server=server, detail=f"docker did not answer within {PROBE_TIMEOUT_S} s")
    if ins.returncode != 0 or not ins.stdout.strip():
        return ProbeResult(True, server=server, detail=f"image {image} not found")
    return ProbeResult(True, True, ins.stdout.strip(), server)


real_docker_probe = docker_probe  # tests replace `docker_probe` with a fake; this keeps the real one reachable

BUILD_TIMEOUT_S = 1200  # first build pulls the base image and installs packages


def build_image(image: str, context: Path | None = None) -> ProcessOutput:
    """Build the sandbox image from `sandbox/`. The only command here that is not read-only, and it only runs when you ask
    (`orch sandbox build`). The caller prints the outcome."""
    context = context or dockerfile_dir()
    if not (context / "Dockerfile").is_file():
        raise SandboxError(f"no Dockerfile in {context}")
    clear_cache()  # a new image changes the answer
    return run_process(build_argv(image, context), cwd=context, env=child_env(), timeout_s=BUILD_TIMEOUT_S)


# ---------- running and cleaning up containers ----------
# Every engine call after the probe goes through these two functions, so a test replaces just them (tests/docker_sim.py).

CALL_TIMEOUT_S = 15  # short engine calls (ps, rm, inspect); a half-started Docker Desktop must not hang a run


def docker_run(
    argv: list[str], *, inner: list[str], src: Path, env: dict[str, str], timeout_s: int, cancel: threading.Event | None = None
) -> ProcessOutput:
    """The long `docker run`. Cancellable. Killing the docker CLI does NOT stop the container; `sweep` does.
    `inner` (the command inside) and `src` (the mounted folder) are not used here: a test double runs `inner` in `src`
    on the host instead of starting a container."""
    return run_process(argv, cwd=Path.cwd(), env=env, timeout_s=timeout_s, cancel=cancel)


def docker_call(argv: list[str], timeout_s: int = CALL_TIMEOUT_S) -> ProcessOutput:
    """A short engine call (`ps`, `rm`, `inspect`). Never cancellable, so cleanup still runs after a cancel."""
    return run_process(argv, cwd=Path.cwd(), env=child_env(), timeout_s=timeout_s)


def _why(out: ProcessOutput) -> str:
    if out.not_found:
        return "the docker command was not found on PATH"
    if out.timed_out:
        return f"docker did not answer within {CALL_TIMEOUT_S} s"
    return _first_line(out.stderr) or out.error or f"exit {out.returncode}"


def _ok(out: ProcessOutput) -> bool:
    return out.returncode == 0 and not (out.not_found or out.timed_out or out.cancelled)


def sweep(run_id: str, log: Callable[[str], None] = lambda m: None) -> list[str]:
    """Force-remove every container (running or stopped) labelled `orch.run=<run_id>`. Returns the ids removed.
    Never raises: a failure is logged with the exact command to check by hand, and the run goes on."""
    label = f"{RUN_LABEL}={_check(run_id, _RUN_ID, 'run id')}"
    ps = docker_call(["docker", "ps", "-aq", "--filter", f"label={label}"])
    if not _ok(ps):
        log(f"warning: could not list sandbox containers ({_why(ps)}); check by hand: docker ps -a --filter label={label}")
        return []
    ids = [i for i in ps.stdout.split() if _CONTAINER_ID.fullmatch(i)]
    if not ids:
        return []
    rm = docker_call(["docker", "rm", "-f", *ids])
    if not _ok(rm):
        log(f"warning: could not remove sandbox containers ({_why(rm)}); remove by hand: docker rm -f {' '.join(ids)}")
        return []
    return ids


@dataclass(frozen=True)
class Container:
    id: str
    run: str  # the orch.run label
    status: str  # docker's own text, e.g. "Up 3 minutes" or "Exited (0) 2 hours ago"


def list_containers() -> list[Container]:
    """Every container with an `orch.run` label, any state. Raises SandboxError when the engine cannot answer."""
    fmt = "{{.ID}}|{{.Label \"" + RUN_LABEL + "\"}}|{{.Status}}"
    out = docker_call(["docker", "ps", "-a", "--filter", f"label={RUN_LABEL}", "--format", fmt])
    if not _ok(out):
        raise SandboxError(f"cannot list sandbox containers: {_why(out)}")
    found: list[Container] = []
    for line in out.stdout.splitlines():
        cid, _, rest = line.strip().partition("|")
        label, _, status = rest.partition("|")
        if _CONTAINER_ID.fullmatch(cid):
            found.append(Container(cid, label, status))
    return found


_clock = time.monotonic
_cache: dict[str, tuple[float, ProbeResult]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def probe(image: str, *, fresh: bool = False) -> ProbeResult:
    """`docker_probe` with a small cache for display. Only a success is kept, and only for CACHE_OK_S seconds.
    `fresh=True` ignores the cache (and still stores a success)."""
    now = _clock()
    if not fresh:
        with _lock:
            hit = _cache.get(image)
        if hit is not None and 0 <= now - hit[0] <= CACHE_OK_S:
            return hit[1]
    res = docker_probe(image)
    with _lock:
        if res.ok:
            _cache[image] = (now, res)
        else:
            _cache.pop(image, None)  # never keep a failure, and a failure invalidates an older success
    return res


@dataclass(frozen=True)
class Status:
    state: Literal["off", "ok", "unavailable"]
    reason: str = ""
    image_id: str = ""
    server: str = ""


def status(sandbox: str, image: str, *, fresh: bool = False) -> Status:
    if sandbox == "off":
        return Status("off")
    res = probe(image, fresh=fresh)
    if res.ok:
        return Status("ok", image_id=res.image_id, server=res.server)
    if not res.engine_ok:
        return Status("unavailable", f"Docker is not running ({res.detail})", server=res.server)
    return Status("unavailable", res.detail, server=res.server)


def unavailable_message(st: Status, target_name: str, image: str) -> str:
    where = f"repo {target_name}" if target_name else "this repo"
    if "not running" in st.reason or "not found on PATH" in st.reason:
        what = f"{st.reason}. Start Docker Desktop and retry"
    else:
        what = f"{st.reason}. Build it with `orch sandbox build`"
    return (f"{what}, or set `sandbox: off` for {where} in config.yaml (tests then run on this machine with your rights). "
            "Nothing was run.")  # fmt: skip


def require(target: ResolvedRepo) -> Status:
    """Called right before anything that can run agent-written code. Always asks the engine again: a cached "ok" from
    a moment ago is not good enough for a run. Returns the status (`off` or `ok`); raises SandboxUnavailable otherwise."""
    st = status(target.sandbox, target.image, fresh=True)
    if st.state == "unavailable":
        raise SandboxUnavailable(unavailable_message(st, target.name or str(target.path), target.image))
    return st


# ---------- one test run ----------
# Exit codes the image's entry script reserves (sandbox/entry.sh). A test that happens to exit with one of them only
# stops the run for you to look at; it can never turn a failure into a pass.
EXIT_VENV_NOT_READY = 96
EXIT_COPY_FAILED = 97
EXIT_KILLED = 137
EXIT_CANNOT_EXEC = (126, 127)
INSPECT_FORMAT = "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}"


@dataclass(frozen=True)
class TestRun:
    """What one sandboxed test run came to. `ran` means the tests ran and `exit_code` is theirs (0 = pass).
    `infra` means the container could not run them (`detail` says why): that is never a test failure."""

    kind: Literal["ran", "infra", "unrunnable", "timed_out", "cancelled"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.kind == "ran" and self.exit_code == 0


TestRun.__test__ = False  # not a pytest class, whatever module imports it

WarmSource = Callable[[], ContextManager[Path]]


def _infra(detail: str, out: ProcessOutput | None = None) -> TestRun:
    return TestRun("infra", out.returncode if out else None, out.stdout if out else "", out.stderr if out else "", detail)


def classify(out: ProcessOutput, name: str, memory: str) -> TestRun:
    """Decide what a finished `docker run` means. A non-zero CLI exit code alone proves nothing (125 can be a daemon
    error or a test calling sys.exit(125); an engine that stops mid-run gives 1), so the engine is asked about the
    container itself. Cancel and timeout are handled by the caller."""
    rc = out.returncode
    if out.not_found:
        return _infra("the docker command was not found on PATH", out)
    if rc is None:
        return _infra(out.error or "docker could not be started", out)
    if rc == 0:
        return TestRun("ran", 0, out.stdout, out.stderr)
    ins = docker_call(["docker", "inspect", "--format", INSPECT_FORMAT, name])
    if not _ok(ins):
        why = _first_line(out.stderr) or _why(ins)  # e.g. "Unable to find image", "error during connect"
        return _infra(f"the container never ran or the engine stopped mid-run ({why})", out)
    parts = ins.stdout.strip().split("|", 3)
    if len(parts) != 4:
        return _infra(f"unreadable answer from docker inspect: {ins.stdout.strip()[:100]!r}", out)
    status, code_s, oom, err = parts
    if err.strip():
        return _infra(f"container could not start: {_first_line(err)}", out)
    if status == "created":
        return _infra("container could not start (it was created but never ran)", out)
    if oom.strip() == "true":
        return _infra(f"out of memory (limit {memory}); raise `sandbox.memory` in config.yaml", out)
    if status != "exited" or code_s.strip() != str(rc):
        return _infra(f"lost contact with the engine (container {status}, exit {code_s.strip()}, docker said {rc})", out)
    if rc == EXIT_KILLED:
        return _infra("container was killed from outside (exit 137, not out of memory)", out)
    if rc == EXIT_COPY_FAILED:
        return _infra("copying the source into the container failed (is /work full?)", out)
    if rc in EXIT_CANNOT_EXEC:
        return TestRun("unrunnable", rc, out.stdout, out.stderr, "test command not found in the sandbox image")
    return TestRun("ran", rc, out.stdout, out.stderr)


def _run_once(spec: Spec, src: Path, command: list[str], run_id: str, volume: str, env: dict[str, str],
              passthrough: tuple[str, ...] | list[str], timeout_s: int, cancel: threading.Event | None) -> TestRun:  # fmt: skip
    name = container_name(run_id)
    argv = test_argv(spec, src, command, run_id, volume, passthrough, name=name)
    out = docker_run(argv, inner=command, src=src, env=env, timeout_s=timeout_s, cancel=cancel)
    if out.cancelled:
        return TestRun("cancelled")
    if out.timed_out:
        return TestRun("timed_out", None, out.stdout, out.stderr)
    return classify(out, name, spec.memory)


def _warm(spec: Spec, run_id: str, volume: str, warm_src: WarmSource, cancel: threading.Event | None,
          log: Callable[[str], None]) -> TestRun | None:  # fmt: skip
    """Prepare the venv volume: the one container with network. Returns None when it worked, else why not.
    `warm_src` hands out a folder holding trusted content only (the run's base commit plus approved dependency files);
    it raises SandboxError when it cannot, for example for a dependency change you have not approved."""
    log("preparing the test environment (network is on for this one container; it runs once per dependency change)")
    with warm_src() as src:
        name = container_name(run_id)
        argv = warm_argv(spec, src, run_id, volume, name=name)
        out = docker_run(argv, inner=WARM_COMMAND, src=src, env=child_env(), timeout_s=WARM_TIMEOUT_S, cancel=cancel)
    if out.cancelled:
        return TestRun("cancelled")
    if out.timed_out:
        return _infra(f"preparing the test environment timed out after {WARM_TIMEOUT_S} s", out)
    res = classify(out, name, spec.memory)
    if res.kind == "ran" and res.exit_code == 0:
        return None
    if res.kind == "ran":
        tail = next((ln.strip() for ln in reversed((out.stderr or out.stdout).splitlines()) if ln.strip()), "")[:200]
        return _infra(f"preparing the test environment failed (exit {res.exit_code}): {tail}", out)
    return TestRun("infra", res.exit_code, res.stdout, res.stderr, f"preparing the test environment failed: {res.detail}")


def run_tests(
    target: ResolvedRepo, cfg: SandboxConfig, src: Path, command: list[str], run_id: str, *,
    warm_src: WarmSource | None = None, timeout_s: int = 600, cancel: threading.Event | None = None,
    log: Callable[[str], None] = lambda m: None, extra_env: dict[str, str] | None = None,
) -> TestRun:  # fmt: skip
    """The one way tests run in the sandbox (the executor and `orch sandbox test` both come through here).
    `src` is mounted read-only; `command` comes from config.yaml. Never raises for a sandbox problem: that is a
    `TestRun(kind="infra")`. Containers of `run_id` are removed on every way out, Ctrl-C included."""
    if target.sandbox == "off":
        raise SandboxError("run_tests called for a repo with sandbox: off")
    try:
        _check(run_id, _RUN_ID, "run id")  # before anything: `sweep` in the finally below needs a good label too
    except SandboxError as e:
        return _infra(str(e))
    try:
        st = require(target)  # asks the engine again; there is no fallback to the host
    except SandboxUnavailable as e:
        return _infra(str(e))
    spec = Spec.for_repo(target, cfg)
    env = {**child_env(passthrough=target.env_passthrough), **(extra_env or {})}  # extra_env: the docker CLI only, never `-e`
    try:
        volume = venv_volume_name(src, st.image_id)
        res = _run_once(spec, src, command, run_id, volume, env, target.env_passthrough, timeout_s, cancel)
        if res.kind == "ran" and res.exit_code == EXIT_VENV_NOT_READY:
            if warm_src is None:
                return TestRun("infra", res.exit_code, res.stdout, res.stderr,
                               "the test environment (venv volume) is not prepared and there is no source to prepare it from")  # fmt: skip
            if (failed := _warm(spec, run_id, volume, warm_src, cancel, log)) is not None:
                return failed
            res = _run_once(spec, src, command, run_id, volume, env, target.env_passthrough, timeout_s, cancel)
            if res.kind == "ran" and res.exit_code == EXIT_VENV_NOT_READY:  # warmed, and still not ready: do not loop
                return TestRun("infra", res.exit_code, res.stdout, res.stderr,
                               "the test environment is still not ready after it was prepared")  # fmt: skip
        return res
    except SandboxError as e:
        return _infra(str(e))
    finally:
        sweep(run_id, log)  # a cancelled docker CLI leaves its container running; this stops and removes it
