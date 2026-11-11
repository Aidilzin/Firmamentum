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
| Docker | Docker Desktop installed, client 29.6.1, **engine not running** during the check | Lowest setup cost of the four; must be started before a run |
| RAM | 15.2 GB | Docker Desktop's VM takes a visible share of it (estimate 2 to 4 GB) |
| Git | 2.56, `credential.helper=manager`, `origin` is an https GitHub URL | `git push` works silently from any process running as you. This is C1's worst case |
| `~/.claude` | contains `.credentials.json` (listed by name only) | On Windows the Pro login is a file in your profile, readable by any process running as you |
| `~/.ssh` | exists | Same |
| `C:\projects` ACL | `Authenticated Users: Modify`, inherited | A second local user could read **and write** `C:\projects\orch`, hooks included (matters for option 3) |
| Worktree root ACL | `%LOCALAPPDATA%\orch`: you, SYSTEM, Administrators only | A second local user could not reach the worktrees at all |
| Your account | member of Administrators (UAC split token) | Nothing here needs elevation except option 3's setup |
| orch source | Windows-only branches in `runlock.py`, `process.py`, `workspace.py`, `budget.py`, `doctor.py`; 4 platform mentions in `tests/` | orch's own suite has never run on Linux. Unknown whether all 320 tests pass there |

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
| Speed (estimates) | Fast on ext4; 1 to 2 s to wake a stopped distro | Tens of seconds per start, one instance at a time | Native | 1 to 3 s per run plus a copy of the tree; Docker Desktop must be up |
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
scratch repos), a non-root user, the entry script. A PHP repo later gets its own image name in config.

### The warm step (the only place with network)

Tests run offline against a prepared `/venv`. orch prepares it with one extra container run that has network and write
access to the venv volume, and runs `uv sync --frozen` on **trusted content only**: the run's base commit, or a tree in
which you approved the dependency change. The volume name carries a hash of `pyproject.toml`, `uv.lock` and the image
id, so an unchanged project reuses it.

The warm step also mounts a named volume `orch-uv-cache` as uv's cache, so a new venv volume (after a dependency
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

**Phase 1: the module.**
1. `sandbox` in the config model, validation, `sandbox: off` warning.
2. `sandbox.py` with `test_argv`; tests assert `--network none`, the read-only mount, no host env, the label, the limits.
3. `sandbox/Dockerfile`, entry script, `orch sandbox build` and `orch sandbox check`.
4. `orch doctor`: engine reachable, image present.

**Phase 2: (b).**
5. `_run_cmd` goes through the sandbox; fail closed when it is unavailable.
6. Kill containers by label on timeout, cancel and exceptions.
7. Warm step with the keyed venv volume; re-warm after an approved dependency change.
8. Skip the retest when sandboxed.

**Phase 3: (c).**
9. Merge candidate with `merge-tree` and `commit-tree`; test in a temp worktree; fast-forward only on pass.

**Phase 4: (a).**
10. Shim writer and `PATH` handling; tests assert the shim text equals `test_argv` and lives outside the worktree.
11. `builder_args` and `repair_args` use `Bash(orch-test)`; prompt text updated.
12. **The one real check** (one `--model sonnet` call, about 10k to 20k tokens): in a scratch worktree of `practice`,
    plant a test that tries to read a canary file on the host, write outside `/work`, and connect to a public address,
    and prints what happened. Prompt: "Run orch-test and reply with its last line." Pass means the Bash call was
    allowed, all three attempts failed, and `permission_denials` is empty. Record it in `docs/probe-results.md`.
13. README "Security model and limits" rewritten for the new boundary.

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
