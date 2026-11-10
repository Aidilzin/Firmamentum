# Phase 5b notes: practice repo, live view, docs

Everything was built and tested with fakes first (255 tests pass, 5 skipped for `tzdata`). Real calls: check A (twice), planner for check B (run by you), then B's builder and reviewer (run by you).

## Design

**Practice repo** (`scripts/make_practice_repo.py`)
- `C:\projects\orch-practice`: uv project `practice`, `slugify` (basic) and deprecated `old_slug`, 5 passing tests, one commit, **committed `uv.lock`** (without it the first `uv run` in a worktree creates one and orch flags `new_dependency`). `pytest` `testpaths=["tests"]`, `pythonpath=["src"]`.
- The hidden tests live in the sibling `C:\projects\orch-practice-hidden\test_slug_extra.py`, not a git repo and never committed (a test checks `git ls-files` and the history). Marker files (`.git/orch-practice-marker`, `.orch-practice-hidden`) mean `--force` only replaces folders the script made. `--hidden-tests` skips dotfiles so the marker is not copied.
- `config.yaml` has `practice:` with `test_command` and `retest_command`.

**`--hidden-tests DIR`** (`exec`, `run`)
- Copied once per run, after the first step's builder and before the post-checks. Files count as planned files (no out-of-plan warning) and are committed with the step. The directory is stored in `meta.json`, so `resume` still applies it.
- Refused before any quota is spent (in the CLI for `run`, before the worktree exists for `exec`): not a directory, empty, subfolders, `DIR` inside the target repo or any orch worktree, or a name already in `tests/` at HEAD (and again against the worktree right before copying).
- The reviewer and repair prompts say the files were added on purpose (reviewer: do not flag as scope, do check the code makes them pass; repair: never delete, rename or edit them). The builder prompt never mentions them.

**Live view** (`ui.py`, rich; display only)
- Table: step, title, status, phase, role, entry (`(fallback)` marked), round, tokens, elapsed; footer with run total tokens and status. Log lines print above the table. A stop panel shows what happened, why, and the `Next:` commands. `--plain` or a non-terminal stdout gives today's line output plus a `--- STOPPED ---` block. The executor only gained an `on_update(meta)` callback fired from `_save()`.
- `Result: done` is printed alone for a finished run (was `Result: done (None): all steps done`).

**Fixes found by the real checks**
- A repair call now always counts as a round and records an outcome (`ok`, `blocked`, `rate_limited`, `failed: <kind>`); `status` shows non-ok outcomes. Not counted: a call that never happened because every entry was cooling. Side effect: a rate-limited repair uses up one round of the budget.
- Run totals: `total_tokens` is planning only; `runs.run_tokens(meta)` returns the sum by role (scout, planner, builder, reviewer, repair) and total. `status`, the live footer and `orch runs` use it; a test checks it equals the sum of every fake call. Older runs show an `unattributed` part.
- `orch diff` for a step stopped at `awaiting_approval`, `needs_input` or `failed` shows its uncommitted worktree diff (git diff HEAD plus new files) under an "UNCOMMITTED" header, with or without `--step`. The stop panel's `Next:` lines include that command while the worktree exists.

## Real-check results

### Check A, attempt 1 (`docs/phase5b-real-run-A.txt`)
Request: "Make slugify in src/practice/text.py handle real world titles". It did not do what the check was for.
- The builder over-delivered; all 18 tests, hidden ones included, passed first time, so there was no test failure and no repair for tests.
- The reviewer (opus) rejected: "unrequested new file" `tests/test_slug_extra.py`. The hidden file was visible in the diff and looked like scope creep.
- Repair (resumed) tried to `rm` the file; **`Bash(rm *)` was denied for real** under `--permission-prompts none`, so it returned `blocked`, and the run stopped at `needs_input`.
- Tokens: planner 46,021, builder 79,722, reviewer 51,017, repair 129,722.
- Led to: the reviewer/repair hidden-file notes, the rounds fix (it showed 0 rounds although a repair ran), the token-total fix (46,021 was the planner only), and the narrower request. Run rejected and cleaned.

### Check A, attempt 2 (`docs/phase5b-real-run-A2.txt`)
Request: "Make slugify in src/practice/text.py strip accents, for example 'Café' becomes 'cafe'". Worked as designed.
- Build, hidden file copied, first test run **failed** (2 of 9: repeated separators, trimming; `.FF......`), repair round 1, retest and full tests pass, review approve (1 minor), commit `874f6920`. No warnings. Not merged.
- **Repair used a real `--resume`.** `repair_raw_2.json` has the same `session_id` (`0875c22e-…`) as `builder_raw.json`, round `session: resumed`, `resume_failed: false`. The resumed session accepted the new `--add-dir` folder and allow-list.
- Reviewer: `approve`, one minor note (behaviour went beyond "keep `.lower().replace(' ', '-')`" because the hidden tests demand it; docstring only mentions accents). The hidden file was not flagged.
- Tokens: planner 44,761, builder 74,019, repair 71,413, reviewer 56,640; run total 246,833 (matches the sum).

### Check B (run by you)
Request: "Remove the deprecated old_slug function and its tests". Run `20261110T152358Z-remove-the-deprecated-old-slug-function`.
- **`tests_removed` fired for real, twice.** First as a plan risk flag (`pre_builder`, planner flagged it), you approved; then as the post-check, listing the exact deleted tests (`tests/test_text.py::test_old_slug_lowercase`, `…::test_old_slug_underscores`); you approved those per file and rule.
- Builder (sonnet) 74,113 tokens, reviewer (opus) 26,703, review `approve`, no repair rounds, no warnings, commit `930be6f0` (src/practice/text.py, tests/test_text.py). Planner 44,840. Run total 145,656.
- The bug that `orch diff` printed nothing at the first stop came from this run (fixed above).
- **`orch merge` on the practice repo:** refused on `main` without the flag, then merged with `--allow-main` (merge commit `a75854c`, branch `orch/20261110T152358Z-…`). Not pushed.

### Answers to the questions in the brief
- **Did repair resume work for real?** Yes, in A1 and A2 (same session id as the builder).
- **Did `tests_removed` fire?** Yes, both as plan flag and as post-check, and per-file approval worked.
- **Any real rate-limit text?** No. No limit was hit in any real run, so `RATE_LIMIT_PATTERNS` and the reset-time parsers are still guesses.
- **Did the denial rules hold?** `rm` was denied in A1, as intended.

## Real tokens per role (from these runs)
| role | A1 | A2 | B |
|---|---|---|---|
| planner (opus) | 46,021 | 44,761 | 44,840 |
| builder (sonnet) | 79,722 | 74,019 | 74,113 |
| reviewer (opus) | 51,017 | 56,640 | 26,703 |
| repair (sonnet, resumed) | 129,722 | 71,413 | 0 |
| total | 306,482 | 246,833 | 145,656 |

## Untested / open
- Real rate-limit messages and reset-time formats (nothing hit a limit); real cooldown scope for opus vs sonnet.
- agy as a reviewer or fallback in a real run; the scout with real agy.
- A repair round after a fallback to a different entry (fresh call path) with real CLIs; `resume_failed` fallback with real CLIs.
- `--hidden-tests` with a multi-step plan against real CLIs (copy once per run is tested with fakes only).
- The live rich view was only tested with a captured console and `--plain`; the real terminal look (resizing, long titles, many steps) was not exercised by me. Your runs used it or plain mode; I have no report either way.
- The reviewer is not told which files are hidden when the hidden-test step is not the step under review (later steps); not an issue for one-step plans.
- Windows only; `tzdata` is still not a dependency.
- Not pushed.
