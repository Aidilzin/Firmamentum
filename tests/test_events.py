import ast
import dataclasses
import threading
import time
from pathlib import Path

import pytest
from plan_factory import good_plan, step
from test_executor import FILE_CMD, PASS_CMD, Boom, edit, env, fail  # noqa: F401  (env is a fixture)
from test_phase4 import ON, review

from orch import budget, events
from orch.adapters.base import AgentResult, ErrorKind
from orch.events import (
    AgentCalled, AgentFinished, EventBuffer, PhaseChanged, RunCrashed, RunFinished, RunStarted, StepFinished, StepStarted,
    Stopped, freeze, thaw,
)
from orch.runs import load_meta, write_meta


def attach(ex):
    got = []
    ex.on_event = got.append
    return got


def kinds(evs):
    """Compact, comparable form: 'RunStarted', 'StepStarted:s1:build', 'PhaseChanged:s1:tests', 'AgentCalled:builder'..."""
    out = []
    for e in evs:
        n = type(e).__name__
        if isinstance(e, StepStarted):
            out.append(f"{n}:{e.step_id}:{e.phase}")
        elif isinstance(e, PhaseChanged):
            out.append(f"{n}:{e.step_id}:{e.phase}")
        elif isinstance(e, (AgentCalled, AgentFinished)):
            out.append(f"{n}:{e.role}")
        elif isinstance(e, StepFinished):
            out.append(f"{n}:{e.step_id}")
        else:
            out.append(n)
    return out


def one_step(setup, **kw):
    return setup(plan=good_plan(steps=[step("s1")]), **kw)


def terminals(evs):
    return [e for e in evs if isinstance(e, events.TERMINAL)]


# ---------- order ----------


def test_normal_run_event_order(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    got = attach(ex)
    assert ex.start().status == "done"
    assert kinds(got) == [
        "RunStarted",
        "StepStarted:s1:build", "AgentCalled:builder", "AgentFinished:builder",
        "PhaseChanged:s1:checks", "PhaseChanged:s1:tests", "PhaseChanged:s1:commit", "StepFinished:s1",
        "StepStarted:s2:build", "AgentCalled:builder", "AgentFinished:builder",
        "PhaseChanged:s2:checks", "PhaseChanged:s2:tests", "PhaseChanged:s2:commit", "StepFinished:s2",
        "RunFinished",
    ]  # fmt: skip
    assert [e.seq for e in got] == list(range(1, len(got) + 1))
    assert len(terminals(got)) == 1 and got[-1] is terminals(got)[0]
    assert got[0].action == "start" and got[0].run_id == run_dir.name
    called, finished = got[2], got[3]
    assert called.entry and called.fallback is False
    assert (finished.tokens, finished.ok, finished.error_kind) == (100, True, None)
    assert got[7].commit == load_meta(run_dir)["steps"]["s1"]["commit"]


def test_review_adds_a_phase_and_a_reviewer_call_pair(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), review("approve")], **ON)
    got = attach(ex)
    assert ex.start().status == "done"
    assert kinds(got) == [
        "RunStarted", "StepStarted:s1:build", "AgentCalled:builder", "AgentFinished:builder",
        "PhaseChanged:s1:checks", "PhaseChanged:s1:tests", "PhaseChanged:s1:review",
        "AgentCalled:reviewer", "AgentFinished:reviewer", "PhaseChanged:s1:commit", "StepFinished:s1", "RunFinished",
    ]  # fmt: skip


def test_repair_round_event_order(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup, test_command=FILE_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"ok.txt": "x\n"})], max_repair_rounds=3)
    got = attach(ex)
    assert ex.start().status == "done"
    assert kinds(got) == [
        "RunStarted", "StepStarted:s1:build", "AgentCalled:builder", "AgentFinished:builder",
        "PhaseChanged:s1:checks", "PhaseChanged:s1:tests", "PhaseChanged:s1:need_repair", "PhaseChanged:s1:repair",
        "AgentCalled:repair", "AgentFinished:repair",
        "PhaseChanged:s1:checks", "PhaseChanged:s1:tests", "PhaseChanged:s1:commit", "StepFinished:s1", "RunFinished",
    ]  # fmt: skip


def test_review_reject_repair_round(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    bad = [{"severity": "major", "file": "src/s1.py", "line": 1, "problem": "bug", "fix": "fix it"}]
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), review("reject", bad), edit({"src/s1.py": "b\n"}),
                                   review("approve")], **ON)  # fmt: skip
    got = attach(ex)
    assert ex.start().status == "done"
    ks = kinds(got)
    assert ks[ks.index("AgentCalled:reviewer"):][:5] == [
        "AgentCalled:reviewer", "AgentFinished:reviewer", "PhaseChanged:s1:need_repair", "PhaseChanged:s1:repair",
        "AgentCalled:repair",
    ]
    assert ks.count("AgentCalled:reviewer") == 2 and ks[-1] == "RunFinished"


# ---------- pause, cancel, resume, crash ----------


def test_pause_then_resume(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [fail(ErrorKind.RATE_LIMITED)])
    got = attach(ex)
    out = ex.start()
    assert out.status == "paused"
    assert kinds(got) == ["RunStarted", "StepStarted:s1:build", "AgentCalled:builder", "AgentFinished:builder", "Stopped"]
    fin, stop = got[3], got[4]
    assert (fin.ok, fin.error_kind, fin.tokens) == (False, "RATE_LIMITED", 0)
    assert stop.outcome == "paused" and stop.step_id == "s1" and stop.meta["resume_after"]
    assert stop.meta["steps"]["s1"]["status"] == "pending"
    budget.clear_cooldowns()
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    got2 = attach(ex2)
    assert ex2.resume().status == "done"
    assert kinds(got2)[:2] == ["RunStarted", "StepStarted:s1:build"] and got2[0].action == "resume"
    assert kinds(got2)[-1] == "RunFinished" and len(terminals(got2)) == 1


def test_all_entries_cooling_is_a_stop_with_no_agent_events(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [fail(ErrorKind.RATE_LIMITED)])
    assert ex.start().status == "paused"
    ex2, fb2 = executor(run_dir, rr, [])  # the cooldown from the first run is still on
    got = attach(ex2)
    assert ex2.resume().status == "paused" and fb2.requests == []
    assert kinds(got) == ["RunStarted", "StepStarted:s1:build", "Stopped"]


def test_cancel_during_a_blocked_call_from_another_thread(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    inside, result = threading.Event(), {}

    def blocking(req):
        inside.set()
        req.cancel.wait(30)
        return AgentResult(agent="claude", ok=False, error_kind=ErrorKind.CANCELLED, text="cancelled")

    ex, _ = executor(run_dir, rr, [blocking])
    got = attach(ex)
    worker = threading.Thread(target=lambda: result.update(out=ex.start()))
    worker.start()
    assert inside.wait(10)
    ex.cancel()
    worker.join(15)
    assert not worker.is_alive() and result["out"].status == "cancelled"
    assert kinds(got) == ["RunStarted", "StepStarted:s1:build", "AgentCalled:builder", "AgentFinished:builder", "Stopped"]
    assert got[3].error_kind == "CANCELLED" and got[3].ok is False
    stop = got[4]
    st = stop.meta["steps"]["s1"]
    assert stop.outcome == "cancelled" and st["status"] == "pending" and st["error"] == "cancelled"
    ex2, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])  # resume after a cancel
    got2 = attach(ex2)
    assert ex2.resume().status == "done" and kinds(got2)[-1] == "RunFinished"


def test_cancel_before_a_step_emits_no_agent_events(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, fb = executor(run_dir, rr, [])
    got = attach(ex)
    ex.cancel()
    assert ex.start().status == "cancelled" and fb.requests == []
    assert kinds(got) == ["RunStarted", "Stopped"]


def test_stop_now_inside_the_call_ends_with_exactly_one_terminal(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    h = {}

    def second_press(req):
        h["ex"].stop_now()
        raise AssertionError("not reached")

    ex, _ = executor(run_dir, rr, [second_press])
    h["ex"] = ex
    got = attach(ex)
    assert ex.start().status == "cancelled"
    assert len(terminals(got)) == 1 and isinstance(got[-1], Stopped) and got[-1].outcome == "cancelled"


def test_crash_recovery_resumes_at_the_stored_phase(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    from test_resume_phase import boom

    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), boom], **ON)
    with pytest.raises(Boom):
        ex.start()
    ex2, fb2 = executor(run_dir, rr, [review("approve")], **ON)
    got = attach(ex2)
    assert ex2.resume().status == "done"
    assert kinds(got) == ["RunStarted", "StepStarted:s1:review", "AgentCalled:reviewer", "AgentFinished:reviewer",
                          "PhaseChanged:s1:commit", "StepFinished:s1", "RunFinished"]  # fmt: skip


def test_crash_after_git_commit_resumes_at_commit(env, monkeypatch):
    repo, setup, executor = env
    from orch import workspace

    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    real = workspace.commit_step

    def commit_then_die(*a, **k):
        real(*a, **k)
        raise Boom("died right after git commit")

    monkeypatch.setattr(workspace, "commit_step", commit_then_die)
    with pytest.raises(Boom):
        ex.start()
    monkeypatch.setattr(workspace, "commit_step", real)
    ex2, fb2 = executor(run_dir, rr, [])
    got = attach(ex2)
    assert ex2.resume().status == "done" and fb2.requests == []
    assert kinds(got) == ["RunStarted", "StepStarted:s1:commit", "StepFinished:s1", "RunFinished"]


# ---------- RunCrashed ----------


def test_an_unexpected_exception_ends_the_stream_with_run_crashed(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    from test_resume_phase import boom

    ex, _ = executor(run_dir, rr, [boom])
    got = attach(ex)
    with pytest.raises(Boom):
        ex.start()
    assert kinds(got) == ["RunStarted", "StepStarted:s1:build", "RunCrashed"]
    crashed = got[-1]
    assert isinstance(crashed, RunCrashed) and crashed.error == "Boom: simulated crash"
    assert crashed.meta["steps"]["s1"]["status"] == "running"  # the last saved state: the toolbar must not say "done"
    assert crashed.meta == freeze(load_meta(run_dir))
    assert len(terminals(got)) == 1


def test_a_crash_emits_the_last_saved_meta_not_unsaved_edits(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)

    def edits_meta_then_dies(req):
        ex.meta["steps"]["s1"]["tokens"] = 999_999  # never saved
        raise Boom("x")

    ex, _ = executor(run_dir, rr, [edits_meta_then_dies])
    got = attach(ex)
    with pytest.raises(Boom):
        ex.start()
    assert got[-1].meta["steps"]["s1"]["tokens"] == 0


def test_preconditions_emit_nothing(env):
    from orch.executor import ExecError

    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    got = attach(ex)
    with pytest.raises(ExecError):
        ex.resume()  # never started
    assert got == []


def test_reject_emits_a_single_stopped(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [fail(ErrorKind.AGENT_ERROR)])
    assert ex.start().status == "failed"
    ex2, _ = executor(run_dir, rr, [])
    got = attach(ex2)
    assert ex2.reject("s1").status == "rejected"
    assert kinds(got) == ["Stopped"] and got[0].outcome == "rejected"


# ---------- snapshots ----------


def test_snapshots_are_read_only_and_independent(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    got = attach(ex)
    ex.start()
    first, last = got[0], got[-1]
    assert first.meta["status"] == "executing" and first.meta["steps"]["s1"]["status"] == "pending"
    assert last.meta["status"] == "done" and last.meta["steps"]["s1"]["status"] == "done"
    with pytest.raises(TypeError):
        first.meta["status"] = "x"
    with pytest.raises(TypeError):
        first.meta["steps"]["s1"]["tokens"] = 1
    assert isinstance(first.meta["steps"]["s1"]["warnings"], tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.seq = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        got[1].step_id = "s9"  # type: ignore[misc]
    token_totals = [e.meta["exec_tokens"] if "exec_tokens" in e.meta else 0 for e in got]
    assert token_totals == sorted(token_totals) and token_totals[-1] == 200  # each event holds the state of its moment


def test_mutating_the_live_meta_after_an_emit_does_not_touch_earlier_snapshots(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    got = attach(ex)
    ex.start()
    snap = got[1]
    before = snap.meta["steps"]["s1"]["title"]
    ex.meta["steps"]["s1"]["title"] = "changed"
    ex.meta["status"] = "changed"
    assert snap.meta["steps"]["s1"]["title"] == before and snap.meta["status"] != "changed"


def test_freeze_thaw_round_trip():
    x = {"a": [1, {"b": [2, 3]}], "c": {"d": None, "e": "s"}, "f": {1, 2}}
    f = freeze(x)
    assert thaw(f) == {"a": [1, {"b": [2, 3]}], "c": {"d": None, "e": "s"}, "f": {1, 2}}
    import json

    assert json.dumps(thaw(freeze({"a": [1, {"b": 2}]}))) == '{"a": [1, {"b": 2}]}'


# ---------- consumers cannot hurt the run ----------


def test_a_buffer_nobody_drains_never_blocks_and_keeps_the_terminal(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    buf = EventBuffer(maxlen=5)
    ex.on_event = buf.emit
    t0 = time.perf_counter()
    assert ex.start().status == "done"
    assert time.perf_counter() - t0 < 60
    assert len(buf) <= 5 and buf.dropped > 0
    last = buf.drain()[-1]
    assert isinstance(last, RunFinished)


def test_a_slow_consumer_thread_does_not_slow_the_executor(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    buf, seen, stop = EventBuffer(maxlen=500), [], threading.Event()

    def slow_consumer():
        while not stop.is_set():
            for e in buf.drain(timeout=0.05):
                seen.append(e)
                time.sleep(0.5)  # a very slow renderer

    t = threading.Thread(target=slow_consumer)
    t.start()
    ex.on_event = buf.emit
    t0 = time.perf_counter()
    try:
        assert ex.start().status == "done"
        took = time.perf_counter() - t0
    finally:
        stop.set()
        t.join(30)
    assert took < 0.5 * 16  # 16 events at 0.5 s each would be 8 s if the executor waited for the consumer


def test_buffer_drops_phase_and_called_events_first():
    def mk(cls, n, **kw):
        return cls(seq=n, run_id="r", meta=freeze({}), **kw)

    buf = EventBuffer(maxlen=4)
    buf.emit(mk(RunStarted, 1, action="start"))
    buf.emit(mk(PhaseChanged, 2, step_id="s1", phase="checks"))
    buf.emit(mk(AgentCalled, 3, step_id="s1", role="builder", entry="claude:sonnet", fallback=False))
    buf.emit(mk(AgentFinished, 4, step_id="s1", role="builder", tokens=1, ok=True, error_kind=None))
    buf.emit(mk(StepFinished, 5, step_id="s1", commit="abc"))  # full: the OLDEST droppable (PhaseChanged) goes
    assert [e.seq for e in buf.drain()] == [1, 3, 4, 5] and buf.dropped == 1
    for e in (mk(RunStarted, 1, action="start"), mk(StepStarted, 2, step_id="s1", title="t", phase="build"),
              mk(AgentFinished, 3, step_id="s1", role="builder", tokens=1, ok=True, error_kind=None),
              mk(StepFinished, 4, step_id="s1", commit="abc")):  # fmt: skip
        buf.emit(e)
    buf.emit(mk(PhaseChanged, 5, step_id="s1", phase="tests"))  # nothing droppable queued: the newcomer is lost
    assert [e.seq for e in buf.drain()] == [1, 2, 3, 4]
    for e in (mk(RunStarted, 1, action="start"), mk(StepStarted, 2, step_id="s1", title="t", phase="build"),
              mk(StepFinished, 3, step_id="s1", commit="abc"), mk(StepStarted, 4, step_id="s2", title="t", phase="build")):  # fmt: skip
        buf.emit(e)
    buf.emit(mk(RunFinished, 5, message="all"))  # only protected events: the oldest one finally goes
    assert [e.seq for e in buf.drain()] == [2, 3, 4, 5]


def test_never_drop_protected_events_while_something_droppable_is_queued():
    def mk(cls, n, **kw):
        return cls(seq=n, run_id="r", meta=freeze({}), **kw)

    buf = EventBuffer(maxlen=3)
    buf.emit(mk(StepStarted, 1, step_id="s1", title="t", phase="build"))
    buf.emit(mk(PhaseChanged, 2, step_id="s1", phase="checks"))
    buf.emit(mk(PhaseChanged, 3, step_id="s1", phase="tests"))
    buf.emit(mk(StepFinished, 4, step_id="s1", commit="c"))
    assert [e.seq for e in buf.drain()] == [1, 3, 4]


def test_a_raising_consumer_never_fails_the_run_and_warns_once(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})], logs=logs)

    def bad(ev):
        raise RuntimeError("renderer bug")

    ex.on_event = bad
    assert ex.start().status == "done"
    assert [m for m in logs if "event consumer failed" in m] == ["event consumer failed: renderer bug"]


def test_without_a_consumer_no_snapshot_is_built(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    import orch.executor as exmod

    def explode(x):
        raise AssertionError("freeze called without a consumer")

    monkeypatch.setattr(exmod, "freeze", explode)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    assert ex.start().status == "done"


# ---------- back-compat and layering ----------


def test_on_update_and_log_still_work(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    logs, updates = [], []
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})], logs=logs)
    ex.on_update = lambda meta: updates.append(meta)
    ex.on_event = lambda e: None
    assert ex.start().status == "done"
    assert updates and all(u is ex.meta for u in updates)  # still the live dict, as before
    assert any(m.startswith("[s1] done") for m in logs) and "[s1] Do s1: running builder" in logs


def test_events_module_imports_no_executor_view_or_ui():
    tree = ast.parse((Path(events.__file__)).read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mods.add((n.module or "") if n.level == 0 else "." + (n.module or ""))
        elif isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
    assert not {m for m in mods if "executor" in m or "view" in m or "ui" in m.split(".")[-1:]}


# ---------- P4b: views are built from snapshots ----------

from io import StringIO  # noqa: E402

from rich.console import Console  # noqa: E402

from orch import cli  # noqa: E402
from orch.executor import ExecOutcome  # noqa: E402
from orch.ui import LiveView, PlainView, render_plain  # noqa: E402
from orch.view import build_run_view  # noqa: E402


def live_view():
    return LiveView({"builder": "claude:sonnet"}, Console(file=StringIO(), width=120, force_terminal=False))


def test_live_view_built_from_a_real_runs_events_matches_the_saved_meta(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    got = attach(ex)
    assert ex.start().status == "done"
    view = live_view()
    for e in got:
        view.on_event(e)
    saved = load_meta(run_dir)
    assert got[-1].meta == freeze(saved)
    assert view.meta is got[-1].meta  # kept as is: frozen, never copied back into a dict
    entries = {"builder": "claude:sonnet"}
    assert render_plain(build_run_view(view.meta, entries)) == render_plain(build_run_view(saved, entries))
    assert build_run_view(view.meta, entries).tokens_total == build_run_view(saved, entries).tokens_total == 200


def test_live_view_tracks_step_times_from_events(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    got = attach(ex)
    ex.start()
    ticks = iter(range(0, 10_000, 3))
    view = LiveView({}, Console(file=StringIO(), width=100, force_terminal=False), clock=lambda: next(ticks))
    for e in got:
        view.on_event(e)
    _, times = view._state
    start, end = times["s1"]
    assert end is not None and end > start


def test_on_update_shim_keeps_no_reference_to_the_callers_dict():
    meta = {"run_id": "r", "status": "executing", "steps": {"s1": {"title": "T", "status": "running", "phase": "build"}}}
    view = live_view()
    view.on_update(meta)
    before = render_plain(build_run_view(view.meta))
    meta["steps"]["s2"] = {"title": "new", "status": "pending"}  # the executor keeps changing its dict
    meta["steps"]["s1"]["status"] = "done"
    assert render_plain(build_run_view(view.meta)) == before
    assert view.meta is not meta and "s2" not in view.meta["steps"]
    with pytest.raises(TypeError):
        view.meta["status"] = "x"


def test_rendering_while_events_arrive_does_not_race(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    got = attach(ex)
    ex.start()
    view = live_view()
    errors, stop = [], threading.Event()

    def refresh_thread():  # what rich's refresh thread does
        while not stop.is_set():
            try:
                view._render()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    t = threading.Thread(target=refresh_thread)
    t.start()
    try:
        for _ in range(300):
            for e in got:
                view.on_event(e)
    finally:
        stop.set()
        t.join(10)
    assert errors == []


def test_live_view_draws_the_final_frame_from_events():
    buf = StringIO()
    view = LiveView({"builder": "claude:sonnet"}, Console(file=buf, width=120, force_terminal=False))
    snap = freeze({"run_id": "r", "status": "executing", "exec_tokens": 77, "role_tokens": {"builder": 77},
                   "steps": {"s1": {"title": "Slugify", "status": "running", "phase": "build", "tokens": 77,
                                    "roles_tokens": {"builder": 77}}}})  # fmt: skip
    with view:
        view.on_event(RunStarted(seq=1, run_id="r", meta=snap, action="start"))
    text = buf.getvalue()
    assert "Maxwell" in text and "building s1" in text and "total 77 tokens" in text


def test_plain_view_ignores_events_and_still_prints_log_lines():
    out = []
    v = PlainView(out.append)
    v.on_event(RunStarted(seq=1, run_id="r", meta=freeze({}), action="start"))
    v.log("[s1] reviewing")
    assert len(out) == 1 and "reviewing" in out[0]


def test_drive_hands_a_live_view_on_event_and_a_plain_view_nothing(monkeypatch, tmp_path):
    seen = {}

    class StubExecutor:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def start(self):
            return ExecOutcome("done", None, "ok")

    monkeypatch.setattr(cli, "Executor", StubExecutor)
    monkeypatch.setattr(cli, "_install_sigint", lambda ex: None)
    monkeypatch.setattr(cli, "_emit_status", lambda *a, **k: None)
    monkeypatch.setattr(cli, "next_commands", lambda *a, **k: [])
    monkeypatch.setattr(cli, "load_meta", lambda d: {})

    class Cfg:
        roles = {}

    class Target:
        name = "demo"

    for plain, expect_events in ((True, False), (False, True)):
        seen.clear()
        monkeypatch.setattr("orch.ui.is_interactive", lambda: True)
        with pytest.raises(Exception):  # typer.Exit
            cli._drive(Target(), tmp_path, Cfg(), "start", plain=plain)
        assert "on_update" not in seen and (seen["on_event"] is not None) == expect_events
