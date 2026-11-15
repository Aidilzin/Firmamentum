"""`sandbox: off` is always visible: run header (rich and plain), status table and the run log."""

import io

from rich.console import Console
from test_executor import PASS_CMD, edit, env  # noqa: F401  (env is a fixture)
from plan_factory import good_plan, step

from orch import cli, sandbox
from orch.runs import create_run, load_meta, write_meta
from orch.theme import resolve_theme
from orch.ui import render_plain, render_rich, sandbox_line
from orch.view import build_run_view

THEME = resolve_theme(cli_theme="dark", env={})
STEPS = {"s1": {"status": "running", "title": "Do s1"}}


def meta(**over):
    return {"run_id": "r1", "status": "executing", "steps": STEPS, **over}


def rich_text(view):
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None, width=120).print(render_rich(view, THEME))
    return buf.getvalue()


# ---------- the view ----------


def test_the_view_carries_the_runs_sandbox_state():
    assert build_run_view(meta(sandbox="off")).sandbox == "off"
    assert build_run_view(meta(sandbox="docker")).sandbox == "docker"
    assert build_run_view(meta()).sandbox is None  # a run that never started, or one stored before the sandbox existed


def test_off_is_drawn_in_the_warning_colour_with_the_warning_glyph():
    line = sandbox_line(build_run_view(meta(sandbox="off")), THEME)
    assert line.plain == "! " + sandbox.OFF_WARNING
    styles = {str(sp.style) for sp in line.spans}
    assert THEME.style("warning", bold=True) in styles and THEME.sym_style("warn") in styles


def test_docker_is_drawn_quietly_and_a_never_started_run_has_no_line():
    line = sandbox_line(build_run_view(meta(sandbox="docker")), THEME)
    assert line.plain == "sandbox: docker" and not any("warning" in str(sp.style) for sp in line.spans)
    assert sandbox_line(build_run_view(meta()), THEME) is None


def test_plain_and_rich_headers_both_show_it():
    off = build_run_view(meta(sandbox="off"))
    assert any(sandbox.OFF_WARNING in ln for ln in render_plain(off, THEME))
    assert sandbox.OFF_WARNING in rich_text(off)
    docker = build_run_view(meta(sandbox="docker"))
    assert "sandbox: docker" in render_plain(docker, THEME) and "sandbox: docker" in rich_text(docker)
    assert not any("sandbox" in ln for ln in render_plain(build_run_view(meta()), THEME))


def test_the_warning_is_in_the_header_even_without_steps():
    view = build_run_view({"run_id": "r", "status": "executing", "sandbox": "off"})
    assert render_plain(view, THEME)[0].endswith(sandbox.OFF_WARNING)


def test_no_colour_still_says_it_in_words():
    mono = resolve_theme(cli_theme="dark", env={"NO_COLOR": "1"})
    assert sandbox.OFF_WARNING in sandbox_line(build_run_view(meta(sandbox="off")), mono).plain


# ---------- status table ----------


def table(tmp_path, **over):
    run_dir = create_run(tmp_path, "x")
    write_meta(run_dir, {"run_id": run_dir.name, "status": "done", **over})
    return cli._status_table(run_dir, "demo")


def test_status_table_warns_for_off_and_names_docker_otherwise(tmp_path):
    assert "WARNING: " + sandbox.OFF_WARNING in table(tmp_path, sandbox="off")
    assert "Sandbox: docker" in table(tmp_path, sandbox="docker")
    assert "andbox" not in table(tmp_path)


# ---------- the executor writes it ----------


def test_an_off_run_logs_the_warning_and_stores_the_state(env, tmp_path):  # noqa: F811
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    rr = rr.model_copy(update={"sandbox": "off"})
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"})], logs=logs)
    assert ex.start().status == "done"
    assert load_meta(run_dir)["sandbox"] == "off"
    assert any(m == "warning: " + sandbox.OFF_WARNING for m in logs)


def test_a_docker_run_stores_docker_and_logs_no_warning(env):  # noqa: F811
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"})], logs=logs)  # ResolvedRepo defaults to docker
    ex.start()
    assert load_meta(run_dir)["sandbox"] == "docker"
    assert not any("sandbox" in m for m in logs)


def test_resume_refreshes_the_state_when_the_config_changed(env):  # noqa: F811
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1"), step("s2", deps=["s1"])]))
    from test_executor import fail

    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"}), fail()])
    ex.start()
    assert load_meta(run_dir)["sandbox"] == "docker"
    off = rr.model_copy(update={"sandbox": "off"})
    logs = []
    ex2, _ = executor(run_dir, off, [edit({"b.py": "2\n"})], logs=logs)
    ex2.resume()
    assert load_meta(run_dir)["sandbox"] == "off" and any(sandbox.OFF_WARNING in m for m in logs)
