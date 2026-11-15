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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


def _hardening(spec: Spec, run_id: str) -> list[str]:
    return [
        "--rm", "--label", f"{RUN_LABEL}={_check(run_id, _RUN_ID, 'run id')}",
        "--user", SANDBOX_USER, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp", "--tmpfs", f"/work:exec,uid={SANDBOX_USER.split(':')[0]},size=2g",
        "--pids-limit", "512", "--memory", spec.memory, "--cpus", str(spec.cpus),
    ]  # fmt: skip


def test_argv(
    spec: Spec, src: Path, command: list[str], run_id: str, venv_volume: str, passthrough: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """`docker run` for one offline test run: no network, source read-only at /src (copied to /work by the image's entry
    script), the prepared venv read-only at /venv. `command` comes from config.yaml, never from a plan."""
    if not command or not all(isinstance(c, str) and c for c in command):
        raise SandboxError("sandbox command must be a non-empty list of strings")
    return [
        "docker", "run", *_hardening(spec, run_id), "--network", "none",
        "--mount", f"type=bind,src={_src_arg(src)},dst=/src,readonly",
        "--mount", f"type=volume,src={_check(venv_volume, _VOLUME, 'volume name')},dst=/venv,readonly",
        "-e", "UV_PROJECT_ENVIRONMENT=/venv", "-e", "UV_NO_SYNC=1", "-e", "UV_OFFLINE=1", "-e", "UV_PYTHON_DOWNLOADS=never",
        "-e", "UV_CACHE_DIR=/tmp/uvc",
        *_passthrough(passthrough),
        "-w", "/work", spec.image, *command,
    ]  # fmt: skip


test_argv.__test__ = False  # not a pytest test, whatever module imports it


def warm_argv(spec: Spec, src: Path, run_id: str, venv_volume: str) -> list[str]:
    """`docker run` for the one step that has network: `uv sync --frozen` into the venv volume, on content you committed or
    approved. The uv cache is a named volume, so a new venv is filled from it without downloading again."""
    return [
        "docker", "run", *_hardening(spec, run_id),
        "--mount", f"type=bind,src={_src_arg(src)},dst=/src,readonly",
        "--mount", f"type=volume,src={_check(venv_volume, _VOLUME, 'volume name')},dst=/venv",
        "--mount", f"type=volume,src={UV_CACHE_VOLUME},dst=/uvcache",
        "-e", "UV_PROJECT_ENVIRONMENT=/venv", "-e", "UV_CACHE_DIR=/uvcache", "-e", "UV_LINK_MODE=copy",
        "-w", "/work", spec.image, "uv", "sync", "--frozen",
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
