# Sandbox design for C1

12 Nov 2026 · branch `v2` at `a8b1b28` · design only, no code was changed and no model call was made.
Follows `docs/audit-fable.md` (C1), `docs/probe-results.md` and `docs/hardening-notes.md`.

## Recommendation in short

Use **Docker Desktop containers for everything the agent can execute**, and keep the `claude` and `agy` processes
themselves on the host under the flags already probed.

- **(b) orch's test runs** and **(c) the post-merge run** go into a container with no network, a read-only copy of the
  source, no host environment and no git metadata.
- **(a) builder and repair**: the `claude` process stays on Windows with `--restricted`. Its one Bash rule changes from
  `Bash(uv run pytest -q)` to `Bash(orch-test)`, a shim that orch generates and that runs the same container. The agent
  then has no way to execute code on the host.
- Putting the `claude` process itself in a box is an optional last phase. It is not where the risk is, and it has a real
  cost (see "The login problem").

Windows Sandbox is not available on this machine. WSL2 and a second Windows user both work but are weaker or cost more
code here. Details below.

## What I checked on this machine

All read-only commands; no credential file was opened.

| Fact | Value | Why it matters |
| --- | --- | --- |
| Windows edition | Windows 11 **Home** Single Language, build 26300 | Windows Sandbox needs Pro, Enterprise or Education |
| WSL | 2.6.3, default version 2, only distro is `docker-desktop` (stopped) | WSL2 option needs a new distro; Docker's backend is already there |
| Docker | Docker Desktop installed, client 29.6.1 at design time (29.9.3 in Phase 0), **engine not running** during the first check, running on the **WSL2 backend** in Phase 0 | Lowest setup cost of the four; must be started before a run |
| RAM | 15.2 GB | Docker Desktop's VM takes a visible share of it (estimate 2 to 4 GB) |
| Git | 2.56, `credential.helper=manager`, `origin` is an https GitHub URL | `git push` works silently from any process running as you. This is C1's worst case |
| `~/.claude` | contains `.credentials.json` (listed by name only) | On Windows the Pro login is a file in your profile, readable by any process running as you |
| `~/.ssh` | exists | Same |
| `C:\projects` ACL | `Authenticated Users: Modify`, inherited | A second local user could read **and write** `C:\projects\orch`, hooks included (matters for option 3) |
| Worktree root ACL | `%LOCALAPPDATA%\orch`: you, SYSTEM, Administrators only | A second local user could not reach the worktrees at all |
| Your account | member of Administrators (UAC split token) | Nothing here needs elevation except option 3's setup |
| orch source | Windows-only branches in `runlock.py`, `process.py`, `workspace.py`, `budget.py`, `doctor.py`; 4 platform mentions in `tests/` | orch's own suite has never run on Linux. Phase 0: 529 of 531 pass on Linux, 1 skip (Windows file sharing), 1 failure (case-sensitive protected paths, fixed in `c089387`) |

## The login problem (why "put claude in the box" is not the first step)

There are two kinds of process, and they need opposite things:

| | Agent process (`claude -p`, `agy -p`) | Agent-written code (pytest, `conftest.py`) |
| --- | --- | --- |
| Who wrote the code | Anthropic / Google | The model, possibly steered by an injected prompt |
| Needs the Pro login | Yes | No |
| Needs network | Yes | No |
| Confined today by | `--restricted`, exact tool rules (probed) | Nothing. This is C1 |

Any box that holds the `claude` process must also hold a login, and the tests that `claude` starts through its Bash tool
run in that same box next to the token. That is true for WSL, Docker and a second Windows user alike. A box around the
agent protects your files, but it puts a fresh credential beside the untrusted code and has to leave the network open.

So the design boxes the second column completely and leaves the first column where its login already is. The hard rule
"never read, copy or use a credential file" also stays simple: nothing is logged in anywhere new, and nothing in the
container can see `~/.claude`.

## The four options

### Comparison

| | 1. WSL2 distro | 2. Windows Sandbox | 3. Second Windows user | 4. Docker container |
| --- | --- | --- | --- | --- |
| Available here | Yes, after creating a distro | **No (Home edition)** | Yes | Yes, installed |
| Default stance | Open: `C:` mounted at `/mnt/c`, Windows `.exe` runnable from Linux | Closed | Open for anything world-readable | **Closed: only what is mounted** |
| Your profile, `~/.claude`, `~/.ssh` | Blocked only after `automount` and `interop` are turned off and tests run as non-root | Blocked | Blocked by profile ACL | Blocked |
| Other repos | Same condition as above | Blocked | **Not blocked**: `C:\projects` is writable by any user until you fix the ACL | Blocked |
| Network | Open. Off only with extra work inside the distro (`bwrap --unshare-net`, nftables) | Off with one config line | Open. Per-user firewall rule possible, needs admin, unverified on Home | **Off with `--network none`** |
| `git push` | Blocked once `C:` and interop are off (no credential manager, no `.git`) | Blocked | Blocked (no stored credential), network still open | Blocked (no `.git`, no credential, no network) |
| Claude Pro login inside | Separate login in the distro, token next to the tests | Lost every time it closes; unusable without copying credentials (forbidden) | Separate login as that user, token next to the tests | Separate login in a volume, token next to the tests. **Not needed in the recommended design** |
| agy login inside | Unverified (Linux build, Google login flow) | Same problem as claude | Separate login as that user | Unverified. Not needed |
| uv cache | Separate Linux cache | Rebuilt on every start | Separate per-user cache and Python | Named volume |
| Windows paths | Worktree must live on ext4; every `workspace.py` call would go through `wsl git` or a `\\wsl.localhost\` UNC path | Mapped folders | **Unchanged** | Unchanged on the host; `/work` inside |
| Tests run on | Linux | Windows | **Windows** | Linux |
| Speed (estimates) | Fast on ext4; 1 to 2 s to wake a stopped distro | Tens of seconds per start, one instance at a time | Native | measured 0.6 s warm start; orch's full suite 23 s with `-n 4`; Docker Desktop must be up |
| Setup cost for you | Medium: distro, `wsl.conf`, users, toolchain, keep it hardened | Windows Pro licence | High: user, password storage, ACL audit, moved worktree root | **Low: start Docker, build one image** |
| Code cost in orch | High (worktree across the boundary) | High (no pipes, results through a folder) | High (spawn as another user) | **Low (an argv prefix)** |

### 1. WSL2

**What it blocks.** Out of the box, nothing: a Linux process can read `/mnt/c/Users/aidil/.claude` and run
`/mnt/c/Windows/System32/cmd.exe` as you. It becomes a boundary only with `/etc/wsl.conf` set to `[automount]
enabled=false` and `[interop] enabled=false, appendWindowsPath=false`, the tests running as a user with no `sudo`, and
root never handed to the agent. Root inside the distro can remount `C:` by hand or re-enable interop, so the boundary is
"no root in the distro", and it can be silently undone by an edit to `wsl.conf`. Network has no per-distro switch.

**What breaks, concretely for this code.** `workspace.create` makes a linked worktree whose `.git` file points at
`C:/projects/orch/.git/worktrees/<id>`. With `C:` unmounted that link is dead, so the distro needs its own clone and orch
has to carry commits across (git bundles through `wsl.exe` pipes). `changed_files`, `removed_tests`, `commit_step`,
`reset_to_head`, `_guard` and `_copy_hidden` all assume a local Windows worktree and would each need a second
implementation. `kill_tree` kills `wsl.exe` but not reliably the Linux children; it would need `wsl --terminate`.

**Spawn.** `wsl.exe -d orch-sbx -u builder --cd <linux path> --exec <argv>`; environment does not cross unless named in
`WSLENV`.

**Verdict.** Good isolation is reachable but you maintain it yourself, and the worktree move touches most of
`workspace.py`. Worth a second look only if Docker Desktop becomes a problem (RAM, licence).

### 2. Windows Sandbox

Not available on Windows 11 Home. On Pro it would be the strongest box (a fresh Hyper-V guest) and a fair fit for (b) and
(c), which need no login. It is a poor fit for (a): the guest is wiped on close, so the login is gone each time, and the
only ways around that are copying or mapping the credential folder, which the hard rules forbid. Output does not come
back on a pipe; results have to be written to a mapped folder and polled. **Rejected.**

### 3. Separate low-privilege Windows user

**What it blocks.** Your profile (so `~/.claude`, `~/.ssh`, the credential manager entry that makes `git push` work).
It does not block the network, and today it does not block `C:\projects`: the ACL there gives every authenticated user
Modify, so the sandbox user could rewrite `C:\projects\orch\.git\hooks` and have code run as you later. You would need to
tighten that ACL, audit every writable folder on `PATH`, and move the worktree root out of your profile to a folder both
accounts can use.

**What breaks.** Spawning. `runas` asks for the password on the console and opens a new window, so orch gets no stdout.
The real options are `CreateProcessWithLogonW` through `ctypes` (orch must store that user's password, plus about 200
lines of handle and pipe code that `subprocess` does not give you), or an OpenSSH server on localhost. A builder running
as that user needs its own Pro login, with the token readable by the tests again.

**What it is good for.** It is the only option that runs tests on Windows. Keep it in mind as a later, per-repo choice for
code whose behaviour is Windows-specific.

**Verdict.** A real boundary on paper, the widest attack surface in practice, and the most code. Not now.

### 4. Docker Desktop containers

**What it blocks.** The container sees one read-only mount of the worktree and nothing else from the host. With
`--network none`, a non-root user, `--cap-drop ALL`, `no-new-privileges` and a read-only root filesystem, a test can
compute and print, and that is all. No `.git` is copied in, so there is nothing to push even if the network were there.
The host environment does not cross; only names in `env_passthrough` are passed with `-e`.

**What breaks.**
- Tests run on Linux. For `practice` and the planned Laravel repo that is fine. For `orch` itself the Windows branches
  (`msvcrt` lock, `taskkill`, the `os.replace` retry) are not exercised by the gate.
- The worktree's `.venv` is a Windows venv. The container uses its own at `/venv` (a named volume), set through
  `UV_PROJECT_ENVIRONMENT`, so the two never mix.
- No network at test time means `uv run` cannot download. The environment is prepared in a separate "warm" step (below).
- `--lf` has no cache between runs unless one is kept (below).
- Docker Desktop has to be running. orch must fail closed when it is not.

**Spawn.** An argv prefix in front of the configured test command, through the existing `run_process`.

**Verdict.** Default-deny, lowest setup, smallest change. **Recommended.**

## The design

### One function builds the sandbox command

New module `src/orch/sandbox.py`, standard library only. Its core is a pure function, so it is fully testable with fakes:

```
test_argv(spec, src: Path, command: list[str], run_id: str, env: dict) -> list[str]
```

which returns:

```
docker run --rm --label orch.run=<run_id>
  --network none
  --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges
  --read-only --tmpfs /tmp --tmpfs /work:exec,uid=1000,size=2g
  --pids-limit 512 --memory 4g --cpus 4
  --mount type=bind,src=<worktree>,dst=/src,readonly
  --mount type=volume,src=orch-venv-<key>,dst=/venv,readonly
  -e UV_PROJECT_ENVIRONMENT=/venv -e UV_NO_SYNC=1 -e UV_OFFLINE=1 -e UV_PYTHON_DOWNLOADS=never
  -e UV_CACHE_DIR=/tmp/uvc          # offline runs still need a writable cache dir on the read-only root
  -w /work orch-sbx:<tag> <test command argv>
```

The image entry script copies `/src` to `/work` (skipping `.git` and `.venv`) and then `exec`s the command. Because the
source mount is read-only and the working copy lives in memory, **a test run can no longer change the worktree**. That
also closes most of H8 (tests rewriting files after the checks) as a side effect.

Rules the module enforces:

- `src` must pass `workspace._guard` (or be a temp worktree orch just created). Never a path read back from `meta.json`
  without that check (M8).
- **Fail closed.** If the repo's sandbox is `docker` and the engine or image is missing, the step stops with a clear
  message. There is no silent fallback to the host. `sandbox: off` must be written explicitly per repo and prints a
  warning on every run.
- **Kill by label.** Killing `docker.exe` does not stop the container. On timeout, cancel or any exception, orch runs
  `docker kill` for every container with `orch.run=<run_id>`. This hangs off the existing cancel path in `run_process`.

Config (top-level default, per-repo override):

```yaml
sandbox: { kind: docker, image: "orch-sbx:py312", memory: 4g, cpus: 4 }
repos:
  practice: { path: "../orch-practice", test_command: "uv run pytest -q", sandbox: docker }
```

The image is a small `sandbox/Dockerfile` in this repo: `python:3.12-slim`, a pinned `uv`, `git` (orch's own tests create
scratch repos), `rsync` (the entry script's copy needs `--exclude`), a non-root user, the entry script. A PHP repo later gets its own image name in config.

### The warm step (the only place with network)

Tests run offline against a prepared `/venv`. orch prepares it with one extra container run that has network and write
access to the venv volume, and runs `uv sync --frozen` on **trusted content only**: the run's base commit, or a tree in
which you approved the dependency change. The volume name carries a hash of `pyproject.toml`, `uv.lock` and the image
id, so an unchanged project reuses it.

The warm step sets `UV_LINK_MODE=copy` (the cache and venv volumes are different filesystems, so hardlinks fail with a
warning). It also mounts a named volume `orch-uv-cache` as uv's cache, so a new venv volume (after a dependency
change or an image rebuild) is filled from the cache instead of downloading everything again. Test runs never download
at all: they are offline and use the prepared `/venv`, so a repair round costs only the container start and the copy.

If a step edits `pyproject.toml` or `uv.lock`, the offline run may fail, and the existing `new_dependency` stop asks you
first. Approving it now means "run this install with network inside the container", after which orch warms a new volume.
Network therefore only ever runs on content you committed or approved.

The project itself is installed editable at warm time and points at `/work/src`, so changed sources are picked up without
a sync. This is the detail most likely to need adjusting; Phase 0 checks it.

### (b) orch's own test runs

`Executor._run_cmd` (`executor.py:897`) keeps its shape. Only the argv changes: `sandbox.test_argv(...)` wraps
`shlex.split(command)`. The output file, exit-code logic and cancel handling stay as they are. Two small wins: POSIX
splitting is now correct because the command runs on Linux (L1), and the per-run timeout also kills the container.

The retest (`--lf`) has no last-failed cache in a fresh container, so it would run the whole suite. Either skip the
retest when sandboxed, or mount a per-run volume at `/work/.pytest_cache`. Start with skipping; it is one line and loses
only a shortcut.

### (c) the post-merge run

Today `merge_run` merges into your checkout, writes `merged_sha`, then runs agent-written tests in your real checkout
(`merge.py:59-68`). New order, which also fixes the second half of M13:

1. Build the merge result without touching the checkout: `git merge-tree --write-tree <target> <branch>`, then
   `git commit-tree` with both parents. (git 2.56 here; needs 2.38 or newer.) A conflict stops here.
2. `git worktree add --detach <temp> <candidate>` under the orch worktree root, run the sandboxed tests on it, remove it.
3. Only if they pass: `git merge --ff-only <candidate>` in the checkout, then write `merged_sha`.

A failing run leaves your branch and working tree exactly as they were. Note this changes behaviour: today a merge with
failing tests is kept and reported.

### (a) builder and repair

The `claude` process stays on the host, logged in as now, with `--restricted --strict-mcp-config`. What changes is the
one thing it may execute:

- At run start orch writes a shim named `orch-test` to `%LOCALAPPDATA%\orch\shims\<run_id>\` (outside the worktree, so
  `--restricted` keeps the file tools away from it). It is a few fixed lines: set `MSYS_NO_PATHCONV=1` (Git Bash would
  otherwise rewrite `/work`), then `exec docker run ...` with exactly the argv from `sandbox.test_argv`, worktree path
  baked in. A second shim `orch-retest` only if the retest is kept.
- `builder_args` allows `Bash(orch-test)` instead of `Bash(<test_command>)`. The shim folder is put first on the child's
  `PATH`. The prompt names `orch-test` as the test command.
- The inner command still comes from `config.yaml` only, never from the plan.

Result: the builder can still iterate on its tests inside one call (no extra quota), but every execution lands in the
container. A `conftest.py` that runs `git push`, reads `~/.ssh` or opens a socket finds no git, no file and no network.

If the shim turns out not to work under `--restricted` (the real check below decides), the fallback is simpler and just
as safe: remove `Bash` from the builder entirely and let the existing repair loop carry test results. It costs more quota
per step, because each failed run becomes a repair call.

agy is unchanged: scout and (opt-in) reviewer are read-only roles and never run tests.

## Phased build plan

Each line is one small commit. Tests use fakes (`run_process` monkeypatched); none of them starts Docker or a real CLI.

**Phase 0: free checks by hand, no code, no quota.** Start Docker Desktop, build the image, and on a scratch export of
`orch` and `practice` confirm: the suite runs in the container (note which orch tests fail on Linux and why); a test
that reads `/src/..`, writes to `/src` or opens a socket fails; the warm step plus `UV_NO_SYNC` picks up an edited
source file; time per run. Write the numbers into this file. Stop and rethink if orch's suite needs more than a handful
of platform skips.

**Phase 1: the module. Done 16 Nov 2026** (seven commits, `Sandbox 1/7` to `7/7`; the commands below are in the README).
1. Config: `sandbox` kind per repo (default `docker`), `image`, top-level `sandbox:` limits; an unconfigured path resolves to `docker`; a bare YAML `off` is read as `"off"`; placeholders `sandbox: "off"` on `orch` and `practice` until Phase 2 (removed in Phase 2).
2. `src/orch/sandbox.py`: pure `test_argv`, `warm_argv`, `venv_volume_name`, `build_argv`; `probe` with a cache that keeps only a success, for at most 30 s; `require`, which always asks the engine again.
3. Fail closed: `services.check_run_request` and `services.drive` (so `run`, `exec`, `resume`, `approve`, `answer`; not `reject`) refuse with exit 2 when Docker or the image is missing.
4. `sandbox: off` in the warning colour in run headers (plain, rich, no colour), `orch status` and the run log; `meta["sandbox"]` is refreshed on every start, resume, approve and answer.
5. `orch doctor`: engine, image per distinct image, one warning row per `off` repo.
6. `sandbox/Dockerfile`, `sandbox/entry.sh`, `.gitattributes` (LF for `sandbox/*`), `orch sandbox build`, `orch sandbox check`.
7. README and this file.

Left for Phase 2, on purpose: the `src` argument of `test_argv` is only checked for mount-safe characters. The rule "must pass `workspace._guard`" belongs where the worktree path is read, in `_run_cmd`. (Done in Phase 2: `Executor._run_sandboxed`.)

**Phase 2: (b). Done 16 Nov 2026** (seven commits, `Sandbox Phase 2, 1/7` to `7/7`). Code and fake tests are in; the hand
checks are in "Phase 2 hand checks" below (all six passed on 16 Nov 2026, after three fixes).
5. `_run_cmd` goes through the sandbox (`sandbox.run_tests`); fail closed when it is unavailable.
6. Kill containers by label on timeout, cancel and exceptions (`sandbox.sweep`); orphan sweep on start, resume, approve and
   answer; doctor warns about orphans.
7. Warm step with the keyed venv volume; re-warm after an approved dependency change.
8. Skip the retest when sandboxed.
9. `orch sandbox test [--probe]`: the same path by hand, plus three planted escape attempts.

What differs from the sketch above:
- **No `--rm`.** A container is named `orch-<run id>-<8 hex>` and removed by `sweep`, so `docker inspect` can still say
  why it stopped (exit code, OOM, never started) after the CLI returns.
- **Exit codes.** The CLI exit code alone proves nothing, so a non-zero exit is checked against the engine:

  | What orch sees | Result |
  | --- | --- |
  | docker not on PATH, engine down, image missing before the run | infra |
  | inspect fails or finds no container (image missing, engine stopped mid-run) | infra, with the first stderr line |
  | container `created` or `State.Error` set | infra: could not start |
  | `OOMKilled` | infra: out of memory (limit), raise `sandbox.memory` |
  | exit 137 without OOM | infra: killed from outside |
  | CLI exit differs from `State.ExitCode`, or the container is not `exited` | infra: lost contact with the engine |
  | exit 97 (entry script: copy failed) | infra |
  | exit 96 (entry script: venv volume not prepared) | warm, then run again; a second 96 is infra |
  | exit 126 / 127 | unrunnable (test command not found in the image) |
  | timeout | failed test (goes to repair, as on the host) |
  | cancel | cancelled |
  | anything else | the tests ran: exit 0 passes, anything else is a test failure |

  An infra result stops the run (`failed`, step `pending`, `stage: sandbox`, `resume_phase: tests`). No repair call is made
  and `rounds_used` is untouched.
- **Warm step source.** The warm container has network and `uv sync` can run build code, so it never mounts the worktree.
  `warm.warm_tree` exports the run's base commit (`git archive`, no `.git`) and lays over it only the dependency files that
  differ in the worktree, each of which needs a `new_dependency` approval from any step of the run. An unapproved change
  stops the run before a container starts. The marker `/venv/.orch-ready` is written last, so a killed warm is never taken
  for a finished one.
- **Orphans.** Containers of a run that outlived a hard-killed orch process are removed before the first test run of the
  next `start`, `resume`, `approve` or `answer` (the run lock proves no live process owns the id). `orch doctor` lists
  containers whose run is not executing with a live lock, and containers from `orch sandbox test`, with the exact
  `docker rm -f` command. It never removes anything.
- **Known limit.** The editable install is made from the base commit. A step that adds a new top-level package in a flat
  (non-`src/`) layout may need a re-warm. `orch` and `practice` use a `src/` layout and are not affected.
- **Still on the host:** the builder's own `Bash(uv run pytest -q)` call (Phase 4). (The post-merge run moved into the
  container in Phase 3.)

### Phase 2 hand checks (no model call; done 16 Nov 2026)

1. `orch sandbox build`, then `orch doctor`: no `off` warning for `orch` and `practice`.
2. `orch sandbox test --repo practice`: the first run warms, then `PASS`; the second does not warm.
3. `orch sandbox test --repo practice --probe`: real tests pass, three `blocked` lines.
4. `orch sandbox test --repo orch`: compare with Phase 0 (529 passed, 1 skipped expected).
5. Cancel (Ctrl-C) and `--timeout 3` during a long run: `docker ps -a --filter label=orch.run` shows nothing afterwards.
6. A wrong `image:` gives `SANDBOX ERROR`, exit 2. One out-of-memory check with `sandbox.memory: 64m` on a scratch copy
   whose test allocates 500 MB: expect "out of memory", not "tests failed". This also shows whether `OOMKilled` is set when
   the killed process is a pytest worker and not PID 1; if it is not, the entry script has to read `memory.events`.

Results (Docker Desktop 29.9.3, image `orch-sbx:py312` 172b737b484d, no model call):

| # | Check | Result |
| --- | --- | --- |
| 1 | `orch sandbox build`, `orch sandbox check`, `orch doctor` | Built; doctor: 19 checks, 0 failed, 1 to look at (the agy login note, unrelated). No `off` warning for `orch` or `practice`, engine and image rows OK. |
| 2 | `orch sandbox test --repo practice`, twice | First run warmed ("preparing the test environment"), 3 passed, `PASS`. Second run did not warm, 3 passed. |
| 3 | `... --probe` | First attempt **found a bug in the probe**: `/mnt` exists, empty, in the base image, so the drive check reported a false `ESCAPED`, and `PROBE FAILED` printed the source line of the assert instead of its message. Fixed (below). After the fix: 6 passed, three `blocked` lines (write: read-only file system on `/src`, `/`, `/etc`, `/work/..`; network: `Network is unreachable`; host env: canary, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `ANTHROPIC_API_KEY` unset and no host drive), `PROBE OK`. |
| 4 | `orch sandbox test --repo orch` | First attempt: 778 passed, 1 failed, 1 skipped. The failure was our own `test_a_dependency_file_that_is_a_symlink_is_refused`, which skipped on Windows and ran on Linux for the first time: the link is refused with "outside the project", not "not a regular file". The test now accepts either refusal and still asserts the refusal. After the fix: **780 passed, 1 skipped (the Windows file-sharing skip), `PASS`, 83 s wall** (Phase 0: 529 tests, 23 s with `-n 4`; this run is serial). The earlier `norm_path` failure from Phase 0 is gone. |
| 5 | Ctrl-C during the `orch` run, then `docker ps -a --filter label=orch.run` | "Stopped by you. The container of this test run was removed." and the listing was empty. `--timeout 3` was covered by the fake-engine test only; not repeated by hand. |
| 6a | Wrong image (`image: orch-sbx:does-not-exist`, scratch config) | `SANDBOX ERROR: image ... not found. Build it with orch sandbox build ... Nothing was run.`, exit 2. |
| 6b | Out of memory: scratch copy of `practice` plus a test that allocates 500 MB, `sandbox.memory: 64m` | `SANDBOX ERROR: out of memory (limit 64m); raise sandbox.memory in config.yaml`, exit 2, not "tests failed". `OOMKilled` is set even though the killed process is a pytest child and not PID 1 (`uv` is PID 1), so the entry script does not need to read `memory.events`. No container left. |

The scratch config and the scratch copy lived in the session scratchpad; the repo's `config.yaml` was not touched and is unchanged.

Fixes made after the hand checks (commit "Sandbox Phase 2 fixes from the hand checks"):
- The probe's drive check is strict but no longer trips on an empty `/mnt`: each of `/c`, `/mnt`, `/mnt/c`, `/mnt/host`,
  `/host_mnt`, `/host_mnt/c` fails only if it is a mount point, a link, not a folder, or holds anything.
- `PROBE FAILED` prints the `E   AssertionError: ESCAPED: ...` line of the failing test (with the real values), not the
  source line of the assert that pytest also shows.
- The symlink test accepts either refusal message. The probe file is copied from the host at run time, so the image did not
  need a rebuild.

Still not checked by hand: `--timeout` on a real container, and the cold start with Docker Desktop stopped.

**Phase 3: (c). Done 16 Nov 2026** (six commits, `Sandbox Phase 3, 1/6` to `6/6`). Fake tests in the commits; the hand
checks are in "Phase 3 hand checks" below (all seven passed, recorded 22 Nov 2026).
9. Merge candidate with `merge-tree` and `commit-tree`; test in a temp worktree; fast-forward only on pass.

What differs from the sketch above:
- **Order.** `orch merge` takes the run's lock, resolves the target and branch shas once, and builds the candidate with
  `git merge-tree --write-tree --name-only -z` plus `git commit-tree` (two parents, the message `Merge orch run: ...`).
  A conflict stops there ("Nothing was changed"). The candidate is checked out as a detached worktree
  `<worktree root>/<repo>/merge-<run id>` with orch's no-hooks options (a tracked `post-checkout` hook would be
  agent-written code on the host), the tests run on it, the worktree is always removed, and only after a pass,
  with the checkout still clean and on the same branch, `git merge --ff-only <candidate>` moves the branch and
  `merged_sha` is written. If you committed meanwhile, the fast-forward refuses and nothing is merged.
- **Already merged by hand.** A branch that is already an ancestor of the target is recorded (`merged_sha`) without
  tests, so a merge made by hand (or an orch crash between the fast-forward and `meta.json`) does not leave `orch status`
  offering "merge" forever.
- **`sandbox: off` repos get the new order too.** One code path; the tests run on the throwaway worktree on the host and
  the run header shows `OFF_WARNING`.
- **Results.** pass: exit 0. Tests fail or time out: exit 1, "Nothing was merged", the path of
  `<run folder>/merge-tests.txt` (full output), and the hint "read `orch diff <run>` first, then `git merge --no-ff
  <branch>`; that manual merge runs this repo's git hooks on this machine". Sandbox problem (engine down, image missing,
  out of memory, command not found in the image) or an unapproved dependency change: `SANDBOX ERROR`, exit 2, nothing
  merged. Conflict and "the checkout changed while the tests ran": error, exit 1. Ctrl-C: nothing merged, exit 130.
- **Warm step for a merge.** Usually none: if the target did not change a dependency file the venv volume is ready. When
  one is needed, the source is your target commit plus the dependency files you approved in this run
  (`warm.merge_warm_tree`). A dependency file whose merged text is neither the target's nor the branch's (both sides
  changed it and git combined them) is refused before any container starts, even if the run's change was approved.
- **Leftovers.** A `merge-<run id>` worktree or a container of the run left by a killed merge is removed at the start of
  the next merge (the run lock proves nobody owns them).
- **`orch doctor`** has a `git version` row: an error below 2.38, a warning when the version cannot be read.
- `workspace.merge_no_ff` and the `MERGE_NOTE` text are gone.
- Behaviour change, as announced in Decision 2: a merge with failing tests is no longer kept. To merge anyway, read the
  diff and run `git merge --no-ff` by hand.

### Phase 3 hand checks (no model call; done, recorded 22 Nov 2026)

All seven passed (Docker Desktop 29.9.3, image `orch-sbx:py312`, git 2.56, no model call, no code change after the checks).
They ran on a scratch **clone** of `practice` (its `origin` removed), never on the real `practice` or `orch`. Each run was a
branch `orch/<id>` with one commit plus a minimal `meta.json` (`status: done`, `exec.branch`, `exec.base_sha`,
`exec.repo_name`), made by a driver script kept in the session scratchpad. Checks 1 to 5 were run by Claude on 16 Nov 2026;
checks 6 and 7 were run by hand by you and are recorded on 22 Nov 2026. Before and after each of 1 to 5: `git rev-parse HEAD`,
`git status`, `git worktree list`, `docker ps -a --filter label=orch.run`.

| # | Check | Result |
| --- | --- | --- |
| 1 | **Pass** | `PASS` (9 passed), "Merged ... as f98b8f03 (not pushed)", exit 0. HEAD `03aed8d` to `f98b8f0`, a two-parent merge commit. Status clean, 1 worktree before and after, no container. |
| 2 | **Fail** (a deliberately failing test on the branch) | `FAIL` with the pytest output, "Nothing was merged; work is still at 03aed8d6", the path of `merge-tests.txt` and the hint "read `orch diff <run>` first, then `git merge --no-ff <branch>`. That manual merge runs this repo's git hooks on this machine." Exit 1. HEAD `03aed8d` before and after, status clean, 1 worktree, no container. |
| 3 | **Conflict** (both sides edit the same line of `src/practice/text.py`) | `Error: merge conflict in: src/practice/text.py. Nothing was changed.`, exit 1, no test run. HEAD `b9f74e1` before and after, status clean, 1 worktree, no container. |
| 4 | **Already merged** (the branch was merged by hand first) | "already part of work; recorded 16bd6ef5. No tests were run.", exit 0. HEAD `16bd6ef` unchanged, status clean, 1 worktree, no container. |
| 5 | **Host untouched** (a branch `conftest.py` that tries six writes: `/src`, `/mnt/c` and `/host_mnt/c` for the repo folder and for `C:\Users\aidil`, and `$USERPROFILE`) | All six blocked inside the container: `/src` gave "Read-only file system", the `/mnt/c` and `/host_mnt/c` paths do not exist, `USERPROFILE` is not set. The merge itself passed (the conftest was a passing test file). On the host `C:\Users\aidil\orch-canary.txt`, `C:\projects\orch-canary-repo.txt` and any `orch-canary*` in the scratch repo or worktree root were all absent. No leftover worktree or container. |
| 6 | **Docker stopped** (`docker desktop stop`, `docker info` failed) | `SANDBOX ERROR: Docker is not running (...). Start Docker Desktop and retry, or set sandbox: off ... Nothing was run.. Nothing was merged.`, exit 2. HEAD `03aed8d` unchanged, 1 worktree. `docker ps` cannot answer while the engine is down; the scratch repo was checked again after the restart (see 7). |
| 7 | **Ctrl-C** (one press, during a test that sleeps 120 s) | "stopping the run (Ctrl-C again: stop now)", then "Stopped by you. Nothing was merged; work is still at 03aed8d6.", exit 130. HEAD `03aed8d` unchanged, `docker ps -a --filter label=orch.run` empty. Your paste did not include `git status` or `git worktree list` for this run; Claude read them afterwards from the scratch repo: status `## work`, one worktree, no container. |

Cosmetic findings (all three fixed on 22 Nov 2026; golden `merge_docker_down`):
- The Docker-down message ended "Nothing was run.. Nothing was merged." (double full stop). The CLI now adds a full stop
  only when the detail has none.
- The engine's error text was cut mid-word at 200 characters ("cannot f)"). `sandbox._clip` cuts at a word boundary and
  marks it with "…".
- With the engine down, `sandbox.sweep` printed one extra warning line before the `SANDBOX ERROR` line. `orch merge` now
  holds sweep warnings back and drops them when the result says the engine is down; with a working engine they still show.

Not checked by hand: the cold-venv warm container for a merge (the venv volume already existed for every run above, so no
warm ran; it is covered by the fake tests only), a second Ctrl-C press, `--timeout` on a real container, and the `sandbox: off`
path on a real repo.

**Phase 4: (a). Done 22 Nov 2026** (six commits, `Sandbox Phase 4, 1/6` to `6/6`, then a docs commit). Fake tests in the
commits; the five hand checks and the one real model check passed and are in "Phase 4 results" below and in
`docs/probe-results.md`.
10. Shim writer and `PATH` handling; tests assert the shim lives outside the worktree and the folders the agent can reach.
11. `builder_args` and `repair_args` use `Bash(orch-test)`; prompt text updated.
12. **The one real check** (one `--model sonnet` call, about 10k to 20k tokens): in a scratch worktree of `practice`,
    plant a test that tries to read a canary file on the host, write outside `/work`, and connect to a public address,
    and prints what happened. Prompt: "Run orch-test and reply with its last line." Pass means the Bash call was
    allowed, all three attempts failed, and `permission_denials` is empty. Record it in `docs/probe-results.md`.
13. README "Security model and limits" rewritten for the new boundary.

What differs from the sketch above:
- **The shim calls back into orch instead of `exec docker run`.** `orch-test` is `exec python -I -m orch.shim <ticket>`, so the
  agent's runs go through `sandbox.run_tests` (engine check of the exit code, unique container names, the exit-96 rule)
  instead of a second, weaker copy of the argv. Under the design's version a sandbox error would have looked like a failed
  test. The ticket (`ticket.json`) is written by orch from `config.yaml` and orch's own state; the runner reads nothing from the
  worktree and validates the worktree again with `workspace._guard`.
- **Runs the same way every time.** Files: `orch-test` (LF only, `#!/bin/sh`, no-arguments guard, `MSYS_NO_PATHCONV=1`),
  `ticket.json`, `calls.jsonl`, in `<worktree root>/../shims/<run id>/`. Rule: exactly `Bash(orch-test)`. The folder goes first
  on the agent's `PATH` through the new `AgentRequest.env_overrides` (merged case-insensitively; `ANTHROPIC_*` refused).
  `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` are set to the runner's timeout plus 120 s (both names are in the Claude
  Code environment-variable docs, in milliseconds).
- **Tamper check.** orch stores a hash of `orch-test` and `ticket.json`, compares them after the call and stops the step
  (`stage: sandbox`) on any change, without using that call's work. Besides, the folder is checked to be outside the worktree
  and the repair input folder, and is deleted after every call.
- **Infra comes from `calls.jsonl` only.** A `blocked` report with a recorded sandbox problem stops the run as `failed`
  (`stage: sandbox`); without a record, a `blocked` report is an ordinary question for you, whatever it says. A test that prints
  `ORCH SANDBOX ERROR` and exits 1 is a failed test.
- **Environment check before each call.** One container run of `true` (through `sandbox.run_tests`, with the warm step behind
  it) before every builder and repair call. If it fails the agent is not called and no quota is spent (`stage: sandbox`; repair
  resumes at `need_repair`).
- **`run_tests(own_only=True)`** removes only the containers that call started, so two `orch-test` calls of one run do not kill
  each other. After every agent call the executor still removes every container of the run by label.
- **The retest is not offered** to a sandboxed repair agent (it is skipped anyway).
- **Probe.** `orch sandbox test --probe` has a fourth test: read a real host canary file outside everything mounted, under every
  spelling Docker Desktop could expose (`/mnt/c/...`, `/host_mnt/c/...`, `/run/desktop/mnt/host/c/...`, `/mnt/host/c/...`, `/c/...`).
- **Known limit.** A test that itself exits with 96 is shown to the agent as the dependency message. orch's own runs treat it the
  same way (it never passes).

Hand checks (no model call, scratch clone of `practice`, driver in the session scratchpad):
1. From Git Bash with the shim's PATH: `orch-test` prints pytest's output and exits 0; a failing test exits 1 with the output.
2. `orch-test -k x` prints "takes no arguments" and exits 2.
3. Edit `uv.lock` in the worktree, run `orch-test`: the dependency message, exit 75.
4. Ctrl-C during a long test, then the executor-style sweep: `docker ps -a --filter label=orch.run` is empty.
5. `orch sandbox test --repo practice --probe`: four `blocked` lines.

The real check (exactly one `--model sonnet` call, scratch clone of `practice` with its `origin` removed, a canary file in the
orch folder, the probe planted as a test, prompt "Run orch-test and reply with its last line."): pass means the reply is the
`N passed` line, `permission_denials` is empty, `calls.jsonl` shows one `ran` with exit 0, the canary is unchanged, nothing new
on the host, and no container is left. Any unexpected result stops the check; no second call without your OK.

### Phase 4 results (22 Nov 2026)

Docker Desktop 29.9.3, image `orch-sbx:py312`, `claude` 2.1.322, scratch clone of `practice` (origin removed), real Git Bash.

| # | Check | Result |
| --- | --- | --- |
| 1 | `orch-test` from Git Bash with the shim's PATH | Found through PATH; 8 passed, exit 0 (4.0 s). A planted failing test gave pytest's output and exit 1. |
| 2 | `orch-test -k x` | "takes no arguments", exit 2. |
| 3 | `uv.lock` edited | The dependency message, exit 75; `calls.jsonl` recorded it as `infra/96`. |
| 4 | Runner killed mid-test, then the sweep | The container outlived the kill by 5 s (so the sweep is needed); the sweep removed it, none left. |
| 5 | `orch sandbox test --repo practice --probe` | Four `blocked` lines, `PROBE OK`. |
| real | One `--model sonnet` call: "Run orch-test and reply with its last line." | Reply `12 passed in 0.03s`; `permission_denials` `[]`; `calls.jsonl` one `ran`/0; host canary sha256 unchanged; no `orch-canary*`/`orch-probe*` file on the host; worktree status only the two planted files; no container left. |

- The shim was found through PATH; the absolute-path fallback was not needed.
- Claude Code ran it in the foreground (one `Bash` call, no `run_in_background`, output in the tool result). The call lasted
  2.5 s, so a run that approaches the 30-minute background default is still unobserved.
- The real check used `--output-format stream-json --verbose` to see the tool calls; the permission flags were the ones orch
  builds. About 15.7k tokens of plan quota.
- Not checked by hand: a real Ctrl-C reaching the runner's own `finally`, the cold-warm path, and a long `orch-test` run.

**Phase 5, optional: box the agent process too.** Only if you want defence against a `--restricted` regression. It means
a second Pro login inside the box, sitting next to whatever the agent runs there, and an egress allow-list. One thing to
look at first: Claude Code has its own sandbox for the Bash tool on Linux and WSL2. I have not checked how it behaves
with `--restricted` on 2.1.322, so treat that as a question for the docs and one probe, not a plan.

## Residual risks, honestly

1. **The `claude` process is still on the host.** Its file tools are held by `--restricted`, a rule inside the vendor
   binary that was probed once on 2.1.322. A CLI update could change it. Have `orch doctor` warn when the `claude`
   version differs from the last probed one, and re-run the two probes then.
2. **A container is one layer.** A kernel, runc or Docker Desktop bug that escapes the container lands in the WSL2 VM,
   from where Windows is reachable as you. This design stops an injected prompt, not someone with a container escape.
3. **Linux is not Windows.** Windows-only code paths are not gated. For `orch` that includes the run lock and process
   kill. You would still run the suite on Windows by hand after a merge, as you, on code you reviewed.
4. **The sandbox ends at the merge.** Once merged, the code is in your checkout and your own `uv run pytest`, editor and
   interactive Claude sessions execute it with your rights. Review and the `executes_later` stop are the gate; the
   sandbox only makes sure nothing runs before that gate.
5. **The warm step has network and runs install code** from the lockfile. It only sees content you committed or approved,
   and no host files, but a dependency approval is now also a "run this with network" approval.
6. **Test output is still untrusted text.** It goes to the repair agent, the reviewer and your terminal. Prompt injection
   through output and terminal escapes (M12) are untouched by this.
7. **Read-only relies on Docker Desktop's file sharing honouring `readonly`** for a Windows path. Phase 0 checks it; I
   have not.
8. **Resource abuse.** Memory, pid and CPU limits bound a runaway test; nothing here bounds quota burned by the agent.
9. **Availability.** Docker Desktop must be running and takes RAM on a 15 GB machine. Fail-closed means a run stops when
   it is down. Its licence is free for personal use; check that still fits you.
10. **agy is outside all of this.** The scout still runs in your main checkout under `--mode plan --sandbox`, with the
    limits written down in `docs/probe-results.md`.

## Decisions (12 Nov 2026)

1. **Docker is a requirement: yes, with conditions.**
   - If Docker is not running or the image is missing, orch stops with a clear message. It never falls back to running
     tests unsandboxed.
   - The only way to run without the sandbox is an explicit per-repo `sandbox: off` in `config.yaml`. `orch doctor` and
     the header of every run show that state in the warning colour, so it is always visible when C1 applies.
   - uv gets a cached volume in the container (`orch-uv-cache`, see the warm step), so repair rounds do not download
     packages again.
2. **Post-merge: as designed.** The merge result is tested first and the branch moves only if the tests pass. After a
   failure nothing is merged automatically; merging by hand after a look is still possible.
3. **The `orch` repo is gated on Linux**, provided Phase 0 shows only a few skips. The `self_hosting` post-check already
   stops any change to `src/orch/`. For the Windows-only paths (run lock, process kill), run the full suite on Windows
   by hand before merging.

## Where this fits in the order

1. Finish P4 to P6.
2. Phase 0 hand checks (free) at any time.
3. Build the sandbox **before** the 6b shell. The shell makes runs easier to start, so C1 should be closed first.

Until the sandbox is built, the rule stays: only your own repos and your own tasks.
