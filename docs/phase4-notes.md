# Phase 4 notes

## Step 0
- **VIRTUAL_ENV**: `adapters/process.child_env()` returns the parent env minus `VIRTUAL_ENV` and `VIRTUAL_ENV_PROMPT`. Used by the claude adapter (plus the 3 `ANTHROPIC_*` vars), the agy adapter (it used to inherit the env unchanged) and every test/retest/post-merge command. PATH is not touched.
- **retest_command**: optional per repo in `config.yaml` (orch: `uv run pytest -q --lf`), config only (the plan model forbids extra fields). Repair rounds run it first; if it fails the round fails without running the full command. The full `test_command` is the only gate before a commit. If unset, the full command is used.

## Step flow
```
build -> checks -> tests -> review -> commit
tests fail / review rejects -> repair -> checks -> retest -> full tests -> review (if on) -> commit
```
- Step states: `pending, running, done, failed, needs_input, awaiting_approval, rejected`. Run states add `rejected`.
- A step that stops stores `stage` (`pre_builder`, `post_check`, `repairs_exhausted`, `builder`, `repair`, ...) and, if it should continue in the middle, `resume_phase` (`tests`, `review`, `need_repair`). Resume, approve and answer continue there; the worktree edits are kept. Only a fresh `pending` step resets the worktree.
- Rounds: the first attempt is round 0. `max_repair_rounds` (default 3) repair calls per set; if the step still fails after the last one: `awaiting_approval`, rule `tests_fail_after_3_repairs`, summary of what is still wrong, nothing committed.
- `tests_removed`: after the builder and after every repair round, `git diff HEAD` is scanned for deleted test files and for removed `def test_*` / `it(`/`test(` functions with no function of the same name added back. Added to the plan enum, `config.yaml approval_rules`, `schemas/plan.json`.
- RATE_LIMITED in builder, review or repair pauses the run (step back to `pending` with its `resume_phase`), never fails it.
- Invalid reviewer output (verdict does not match the severities): one retry (resume the reviewer session, else a fresh call with the bad output), then the step is `failed`. Nothing is ever committed unreviewed when review is on.
- No diff (nothing changed) skips the review.

## Reviewer
- `roles/reviewer.md`, schema `schemas/review.json` generated from `review.py` (drift test). Validator: any blocker/major <=> `reject`. Minor issues are stored in `meta.json` and shown by `orch status`, never block.
- claude: `--tools Read,Grep,Glob --permission-prompts none --add-dir <steps/sN> --append-system-prompt <role>`; no Edit/Write/Bash. agy: `--mode plan --add-dir <steps/sN>`, role text in the prompt (agy has no system-prompt flag and no tool allow-list; plan mode is the only guard).
- The uncommitted step diff is written to `steps/<id>/diff.patch` before the call and its path is in the prompt. After the commit the file is overwritten by the committed diff.
- `review: per_step | off` in `config.yaml` (default per_step).

## Repair
- `roles/repair.md`: the v1 "Descartes" idea (root-cause traceback analysis, fix only what failed) plus the builder's rules and "never weaken/delete tests". The old fenced-code reply format is gone; it answers with the builder JSON report so it can set `blocked`.
- Same tools and deny list as the builder; the Bash allow-list gets both `test_command` and `retest_command`.
- **`--add-dir` is NOT the step folder.** Each round orch copies `diff.patch`, `test_tail.txt` (last 150 lines) and `review_issues.json` into `steps/<id>/repair_input/round<N>/` and passes only that folder. Orch never reads anything back from it. (The reviewer keeps `--add-dir` on the step folder because it has no Edit/Write.)
- Tries `--resume <builder or last repair session>`; the prompt then starts with a short "REPAIR mode" rules block because a resumed session keeps its original system prompt. If the resumed call fails to run (anything except RATE_LIMITED) it falls back to one fresh call. `rounds[i].session` is `resumed` or `fresh`, with `resume_failed`.

## Human commands
- `orch approve <run> [--step sN] [--retry]`
  - `pre_builder` (plan risk flag): runs the builder now.
  - `post_check` (protected path / dependency / tests removed): **approval is per file and rule.** The exact flagged items are stored in `approved`; post-checks still run every round and only a NEW item stops again. Continues at tests -> review -> commit.
  - `repairs_exhausted`: plain approve is refused ("never commits failing tests"); `--retry` resets the per-set counter (`rounds_total` keeps counting) and continues at repair.
  - Anything else (failed, needs_input, rejected, done) is refused with a message.
- `orch reject <run> --step sN`: step `rejected`, run `rejected`; resume/approve/answer then refuse.
- `orch answer <run> [--step sN] "<text>"`: plan `needs_input` -> re-plan with a Clarification block appended to the request (old raw output kept as `planner_raw.N.json`, token total accumulated, does not auto-exec). Step `needs_input` -> builder re-run with the answer in the prompt (or repair continues when repair was the one blocked), then the run continues.
- `orch merge <run> [--allow-main]`: refuses if the run is not `done`, already merged, main repo dirty, detached HEAD, or current branch is main/master without `--allow-main`. `git merge --no-ff -m "Merge orch run: <request, 60 chars>"`; conflict -> `merge --abort`. Then runs the full `test_command` in the main repo and reports; a failing result keeps the merge and prints how to undo it. Never pushes.
- Every stop prints `Next: <exact command>`; `orch status` has a "Needs from you" block plus rounds, verdicts, issues and tokens per role.

## Real check
`uv run orch run "Add a --json flag to orch usage ..."` on this repo, planner opus, builder sonnet, reviewer opus, review on. Raw console output: `docs/phase4-real-run.txt`. Exit 0, not merged, not pushed.

- **Plan**: 1 step, [s1] add `--json` option to `usage` in `src/orch/cli.py` + 2 tests in `tests/test_budget_cli.py`; no risk flags. Planner: 78,001 tokens.
- **s1**: done, commit `ba363d52`. Builder 120,257 tokens (2 calls total incl. review; builder 9 turns, 163 s, $0.11 list). Full tests passed (131.6 s). No warnings.
- **Review (opus, round 0)**: `approve`, 0 blocking, 0 minor issues. 86,257 tokens, 6 turns, 11 s, $0.26 list. It read the diff file via `--add-dir`.
- **Repair rounds**: 0. Repair, retest, resume, approve and tests_removed paths were NOT exercised by the real run (only by fake-adapter tests).
- **Tokens by role**: planner 78,001; builder 120,257; reviewer 86,257; repair 0. Total 284,515.
- Diff vs v2: `src/orch/cli.py` (+17/-2), `tests/test_budget_cli.py` (+16). Review it with `orch diff <run> --repo orch` before merging.

## Untested
- Real `--resume` for repair (and whether a resumed session honours a changed `--add-dir` / allow-list).
- That `--add-dir` really makes the folder readable under `--tools Read,Grep,Glob` with `--permission-prompts none`, and that the reviewer cannot write (taken from help text unless the real check covered it).
- agy as reviewer (`--mode plan --add-dir`), and whether agy honours `--json-schema` there.
- Whether `--add-dir` (variadic) followed by `--append-system-prompt` parses as intended on the real claude CLI.
- Real rate-limit text during review/repair.
- `tests_removed` for languages other than Python/JS-style `it(`/`test(`; renamed tests count as removed unless the same name is added.
- A failing `retest_command` that cannot start (not found) fails the step instead of repairing.
- Windows-only paths; non-Windows untested.
