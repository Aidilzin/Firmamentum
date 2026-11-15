# Sandbox Phase 0: results

15-16 Nov 2026 · branch `v2` at `5dbdd86` · hand checks only. No orch code changed, no model call made.
Checks followed `docs/sandbox-design.md`. Scratch files (Dockerfile, source exports) lived in the session scratchpad,
not in the repo. Source was exported with `git archive HEAD`, so the tests ran against committed content only.

## Verdict

**The design holds.** No change is needed to its structure. Five small points need adjusting, listed under
"Changes to the design" below. Only one orch test fails on Linux, and the cause is a real (small) bug, not a platform
quirk.

## Results

| Check | Result |
| --- | --- |
| Docker Desktop running | Yes. Server 29.9.3, `Docker Desktop`, kernel `6.6.87.2-microsoft-standard-WSL2`. **WSL2 backend** (the only WSL distro is `docker-desktop`, Running). The design said the engine was stopped; it was already up. |
| VM memory | 7.9 GB visible to Docker (host has 15.2 GB) |
| Image | `python:3.12-slim` + `uv 0.13.7` + `git` + `rsync`, non-root uid 1000, entry script. Builds in **35 s** (images already pulled once), **380 MB** |
| Container start, warm | **0.6 s** (3 runs: 0.61, 0.59, 0.61), with the full lock-down flags |
| Container start, first after build | 1.1 s. I did not measure with the engine stopped; it was running throughout, so a true cold engine start is untested |
| `practice` tests in container | 3 passed, 0 failed, 0 skipped. **1.9 s** per run including start and copy. Windows: 1.5 s |
| `orch` tests in container | `-n 4`: **529 passed, 1 failed, 1 skipped** in 20 s inside pytest, **23 s** wall. Serial: same counts, 64 s inside pytest, 68 s wall. `-m "not slow"`: 217 passed, 0 failed, 11 s inside pytest, 15 s wall |
| `orch` tests on Windows | `-n 4`: **531 passed** in 167 s wall. Quick loop is about 12 s per CLAUDE.md |
| Runtime, Linux vs Windows | Linux container is about **7x faster** with `-n 4` (23 s against 168 s). I did not profile why; the suite spawns many git processes, which are likely slower on Windows |
| Suite size | 531 tests, not the 320 the design assumed |

### The one failure and the one skip

- `tests/test_executor.py::test_default_protected_paths` fails on `Secrets.yaml`. Cause: `checks.norm_path` relies on
  `os.path.normcase` to lower-case paths, which does nothing on Linux. The default patterns are lower case, so on Linux
  a file named `Secrets.yaml` or `.ENV` is **not** classed as protected. On Windows (where orch runs today) it works. It
  is a latent bug in orch, and it matters if orch ever runs on Linux or a repo is checked out case-sensitively.
  Suggested fix: use `.lower()` instead of `normcase` in `norm_path`.
- `tests/test_runlock.py:126` skips itself ("Windows file sharing"). This is a deliberate skip in the test, not a
  surprise.
- Nothing else in `runlock`, `process`, `workspace`, `budget` or `doctor` fails on Linux. The 4 platform mentions in
  `tests/` are already handled by skips.

The design said to stop and rethink if the orch suite needs "more than a handful of platform skips". It needs one skip
and one fix. Decision 3 (gate the `orch` repo on Linux) stands.

### Isolation probes (container flags as in the design)

| Attempt | Result |
| --- | --- |
| Write to the source mount (`touch /src/x`) | `Read-only file system`. Docker Desktop **does honour `readonly`** on a Windows path (residual risk 7 is cleared) |
| Write to `/etc` | `Read-only file system` |
| Open a socket to `1.1.1.1:53` | `Network is unreachable` |
| Host env visible | 0 matches for `anthropic`, `aidil`, `userprofile` in the container env |
| Host drives | No `/c`, nothing under `/mnt` |
| Identity | `uid=1000(builder)`, all capabilities dropped |
| Reach files outside the copy | `/src/..` is the container root, not the host |

I did not plant a canary file in a host folder and try to read it. Nothing from the host is mounted other than the
source, so the above covers it.

### Warm step, uv cache, editable source

- `uv sync --frozen` into a named venv volume works with network on, the source read-only, and `/work` as a tmpfs.
  `practice`: 2.3 s the first time, 0.9 s with a fresh venv volume and a warm cache. `orch`: 5.7 s on the first run
  (downloaded `pydantic-core`), **2.0 s with a fresh venv volume**, and no `Downloaded` line. So the `orch-uv-cache`
  volume works: the second run did not download anything.
- The offline run (`--network none`, `UV_OFFLINE=1`, `UV_NO_SYNC=1`, venv volume mounted read-only) works. `uv run` does
  not try to sync or download.
- **Edited source is picked up without a re-sync.** I appended `MARK = 42` to `src/orch/__init__.py` in the export and ran
  `import orch` offline. It printed `MARK 42` from `/work/src/orch/__init__.py`. The editable install points at
  `/work/src`, and the entry script's copy lands there each run. This was the detail the design flagged as most likely to
  need adjusting; it needs none.

## Changes to the design

1. **Image needs `rsync`** (or the entry script uses `tar`/`cp`). I used `rsync -a --exclude .git --exclude .venv`.
   `cp -r` cannot exclude, so keep `rsync` or use `tar --exclude`.
2. **Set `UV_LINK_MODE=copy` in the warm step.** The cache volume and the venv volume are different filesystems, so uv
   warns that hardlinking failed and copies anyway. It costs nothing but noise; setting it silences the warning.
3. **Tmpfs for uv's cache at test time.** Offline runs still want a writable cache dir. I set `UV_CACHE_DIR=/tmp/uvc`
   on the read-only root. Add it to the `-e` list in `test_argv`.
4. **Fix `norm_path` before relying on the Linux gate** (see the failure above). It is one line, and it fixes a case where
   the gate would treat `Secrets.yaml` as safe.
5. **Numbers in the doc are out of date.** The suite is 531 tests (not 320) and Docker is 29.9.3 (not 29.6.1). The
   design's "1 to 3 s per run" estimate was pessimistic: a warm start is 0.6 s, and a full gated orch run is 23 s, so
   repair rounds are cheap.

## Not covered by Phase 0

- `practice-hidden` was not run; Phase 0 only listed `orch` and `practice`.
- Engine cold start (Docker Desktop stopped) and the cost of Docker's RAM share while idle.
- The `orch-test` shim under `--restricted` (Phase 4, step 12; the one real model call).
- Docker with `--lf`: not tried; the design already skips the retest when sandboxed.

## Reproduce

Image (scratch, `img/Dockerfile`):

```
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends git rsync && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m builder && mkdir /venv /uvcache && chown 1000:1000 /venv /uvcache
COPY entry.sh /entry.sh
RUN chmod +x /entry.sh
USER 1000:1000
ENTRYPOINT ["/entry.sh"]
```

`entry.sh`: `rsync -a --exclude .git --exclude .venv --exclude __pycache__ /src/ /work/; cd /work; exec "$@"`.

Test run: the `test_argv` flags from the design plus `-e UV_CACHE_DIR=/tmp/uvc`, then `uv run pytest -q -n 4`.
