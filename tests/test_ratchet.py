"""The quality ratchet: a change may make the code better or leave it as it is, never worse without a reason.

Ported from tv-mcp's, which came from claude-voice's. The layers among the
modules are counted here too, so that the code-health page can show them, and
`tests/test_layers.py` refuses a breach outright.
"""

import contextlib
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).parent.parent


def _load():
    """The ratchet, loaded by path: scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("ratchet", ROOT / "scripts/ratchet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ratchet = _load()

BASE = {
    "coverage": 85.0,
    "mutation": {"killed": 20, "total": 20},
    "tests": 400,
    "lint_errors": 0,
    "type_errors": 0,
    "cycles": 0,
    "layer_violations": 0,
    "duplication": 2.0,
    "complexity": {"pkg/a.py": 10, "pkg/b.py": 5},
    "test_seconds": 40.0,
    "test_budget": {"seconds": 100, "tolerance": 0.25},
}
BUDGET = {"seconds": 100, "tolerance": 0.25}


def measured(**changes):
    return {**json.loads(json.dumps(BASE)), **changes}


def problems(base, now, exceptions=()):
    return ratchet.compare(base, now, set(exceptions), BUDGET)


# -- what gets worse ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "metric"),
    [
        ({"coverage": 83.0}, "coverage"),
        ({"mutation": {"killed": 19, "total": 20}}, "mutation"),
        ({"tests": 399}, "tests"),
        ({"lint_errors": 1}, "lint_errors"),
        ({"type_errors": 2}, "type_errors"),
        ({"cycles": 1}, "cycles"),
        ({"layer_violations": 1}, "layer_violations"),
        ({"duplication": 3.0}, "duplication"),
        ({"complexity": {"pkg/a.py": 11, "pkg/b.py": 5}}, "complexity"),
    ],
)
def test_a_measure_that_gets_worse_fails_the_check(change, metric):
    found = problems(BASE, measured(**change))
    assert found
    assert all(metric in p for p in found)


def test_coverage_may_wobble_a_little_between_machines():
    assert problems(BASE, measured(coverage=84.7)) == []


def test_a_new_module_starts_its_own_baseline():
    now = measured(complexity={"pkg/a.py": 10, "pkg/b.py": 5, "pkg/c.py": 12})
    assert problems(BASE, now) == []


def test_a_new_module_must_keep_every_function_at_fifteen_or_below():
    now = measured(complexity={"pkg/a.py": 10, "pkg/b.py": 5, "pkg/new.py": 16})
    found = problems(BASE, now)
    assert found
    assert "pkg/new.py" in found[0]
    assert problems(BASE, measured(complexity={"pkg/a.py": 10, "pkg/b.py": 5, "pkg/new.py": 15})) == []


def test_a_base_without_a_new_measure_has_nothing_to_compare_with():
    base = {k: v for k, v in BASE.items() if k != "duplication"}
    assert problems(base, measured(duplication=9.0)) == []


def test_a_measure_may_get_worse_when_the_change_says_why():
    now = measured(coverage=80.0, tests=390)
    assert problems(BASE, now, exceptions={"coverage", "tests"}) == []
    assert problems(BASE, now, exceptions={"coverage"}) != []


def test_an_exception_is_written_in_the_pull_request_as_a_line_of_its_own():
    body = (
        "Some text.\n\nRatchet-Exception: coverage - the old module was deleted\n"
        "ratchet-exception: tests: merged two\n"
    )
    assert ratchet.exceptions(body) == {"coverage", "tests"}
    assert ratchet.exceptions("We talked about a Ratchet-Exception: coverage once.") == set()
    assert ratchet.exceptions(None) == set()


# -- the baseline moves with the code ----------------------------------------------------


def test_a_pull_request_need_not_record_the_levels_it_reached():
    better = measured(coverage=88.0, tests=410, complexity={"pkg/a.py": 8, "pkg/b.py": 5})
    assert problems(BASE, better) == []


def test_the_suite_must_stay_inside_its_time_budget():
    assert problems(BASE, measured(test_seconds=124.0)) == []  # inside the tolerance
    found = problems(BASE, measured(test_seconds=126.0))
    assert found
    assert "test_seconds" in found[0]


def test_the_time_budget_is_the_one_thing_quality_json_keeps(tmp_path):
    path = tmp_path / "quality.json"
    path.write_text(json.dumps({"test_budget": {"seconds": 90, "tolerance": 0.1}}))
    assert ratchet.budget(path) == {"seconds": 90, "tolerance": 0.1}
    assert ratchet.budget(tmp_path / "missing.json") == ratchet.DEFAULT_BUDGET
    assert set(json.loads((ROOT / "quality.json").read_text())) == {"test_budget"}


def test_measured_levels_are_written_for_main_to_keep(tmp_path):
    out = tmp_path / "levels.json"
    ratchet.write_levels(out, {**measured(), "test_budget": None})
    assert json.loads(out.read_text()) == {k: v for k, v in BASE.items() if k != "test_budget"}


def test_the_levels_main_kept_are_the_base_when_they_are_there(tmp_path):
    kept = tmp_path / "levels.json"
    kept.write_text(json.dumps(BASE))
    asked = []
    assert ratchet.base_levels(kept, lambda: asked.append(1) or {}) == BASE
    assert asked == []


def test_when_the_levels_main_kept_are_missing_the_base_is_measured(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    for kept in (None, tmp_path / "missing.json", tmp_path / "broken.json"):
        assert ratchet.base_levels(kept, lambda: BASE) == BASE


# -- what is measured --------------------------------------------------------------------


def test_complexity_is_counted_per_function_and_a_nested_function_counts_on_its_own(tmp_path):
    source = textwrap.dedent(
        """
        def outer(x):
            if x:
                return 1
            def inner(y):
                for i in y:
                    if i and y:
                        return i
            return inner

        def flat():
            return 0
        """,
    )
    (tmp_path / "m.py").write_text(source)
    assert ratchet.complexity(tmp_path) == {"m.py": 4}  # inner: 1 + for + if + and


def _package(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_a_cycle_among_the_modules_is_counted_and_a_lazy_import_is_not(tmp_path):
    pkg = tmp_path / "pkg"
    _package(
        pkg,
        {
            "__init__.py": "",
            "a.py": "from . import b\n",
            "b.py": "from .a import thing\n",
            "c.py": "def later():\n    from . import a\n",
            "d.py": "from . import c\n",
        },
    )
    assert ratchet.cycles(pkg) == 1


def test_a_cycle_through_a_subpackage_by_absolute_imports_is_counted(tmp_path):
    pkg = tmp_path / "pkg"
    _package(
        pkg,
        {
            "__init__.py": "",
            "core.py": "from pkg.drivers.tv import Tv\n",
            "drivers/__init__.py": "",
            "drivers/tv.py": "import pkg.core\n",
            "tools/__init__.py": "from pkg.tools import say\n",
            "tools/say.py": "from pkg import core\n",
            "other.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg import core\n",
        },
    )
    assert ratchet.cycles(pkg) == 1


def _layered(tmp_path, files):
    """A package named like this one, so its imports read as they do in src/."""
    pkg = tmp_path / "easee_mcp"
    _package(pkg, {"__init__.py": "", **files})
    return pkg


def test_an_import_a_layer_may_not_use_is_a_breach(tmp_path):
    allowed = {
        "adapter.py": "import json\n",
        "cache.py": "from easee_mcp.adapter import SlowDownError\n",
        "server.py": "from mcp.types import ToolAnnotations\nfrom easee_mcp.cache import CachedCharger\n",
        "cli.py": "from easee_mcp.server import build_server\n",
    }
    assert ratchet.layer_breaches(_layered(tmp_path, allowed)) == []
    wrong = {
        "cache.py": "from easee_mcp.server import build_server\n",
        "answers.py": "def later():\n    import mcp.types\n",
    }
    assert ratchet.layer_breaches(_layered(tmp_path / "b", {**allowed, **wrong})) == [
        "answers (core) imports mcp.types",
        "cache (core) imports easee_mcp.server (top)",
    ]


def test_a_module_with_no_layer_is_a_breach(tmp_path):
    assert ratchet.layer_breaches(_layered(tmp_path, {"stray.py": ""})) == ["stray has no layer"]


def test_duplicated_code_is_the_share_of_lines_in_blocks_that_appear_twice(tmp_path):
    block = "\n".join(f"x{i} = compute({i})" for i in range(6))
    (tmp_path / "a.py").write_text(
        block + "\n\nunique_a = 1\n" + "\n".join(f"y{i} = {i}" for i in range(3)) + "\n"
    )
    (tmp_path / "b.py").write_text("    " + block.replace("\n", "\n    ") + "\nunique_b = 2\n")
    assert ratchet.duplication(tmp_path) == round(100 * 12 / 17, 2)  # 12 of 17 non-blank lines


def test_the_mutation_tally_is_read_from_the_mutation_scripts_last_line():
    out = "KILLED  a rule: 1\n\n7/9 rules turned off the test that names them\n"
    assert ratchet.mutation_tally(out, returncode=1) == {"killed": 7, "total": 9}
    full = "9/9 rules turned off the test that names them\n"
    assert ratchet.mutation_tally(full, returncode=0) == {"killed": 9, "total": 9}
    # A run that failed for any other reason is never a full score.
    assert ratchet.mutation_tally(full, returncode=1) == {"killed": 8, "total": 9}
    assert ratchet.mutation_tally("Traceback ...", returncode=1) == {"killed": 0, "total": 1}


STOPPED_WHILE_MEASURING = textwrap.dedent(
    """
    import importlib.util, os, signal, sys, tempfile
    spec = importlib.util.spec_from_file_location("ratchet", sys.argv[1])
    ratchet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ratchet)
    ratchet.stop_cleanly()
    try:
        with tempfile.TemporaryDirectory() as copy:
            open(os.path.join(copy, "levels.json"), "w").close()
            os.kill(os.getpid(), int(sys.argv[2]))
    finally:
        stops = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
        print(all(signal.getsignal(s) == signal.SIG_IGN for s in stops))
    """
)


def _stop_while_measuring(tmp_path, signum):
    temp = tmp_path / "temp"
    temp.mkdir()
    done = subprocess.run(
        [sys.executable, "-c", STOPPED_WHILE_MEASURING, str(ROOT / "scripts/ratchet.py"), str(int(signum))],
        env={**os.environ, "TMPDIR": str(temp)},
        capture_output=True,
        text=True,
        check=False,
    )
    return done, sorted(p.name for p in temp.rglob("*"))


@pytest.mark.parametrize(
    "signum",
    [signal.SIGINT, signal.SIGTERM, signal.SIGHUP],
    ids=["interrupted", "cancelled", "hung up"],
)
def test_a_stopped_quality_run_removes_its_copies(tmp_path, signum):
    """CI cancels with SIGTERM and a closed terminal sends SIGHUP; both skip every `finally` by default."""
    done, left = _stop_while_measuring(tmp_path, signum)
    # Ctrl-C ends as Python ends any uncaught KeyboardInterrupt: by its own SIGINT.
    assert done.returncode == (-signum if signum == signal.SIGINT else 128 + signum), done.stderr
    assert left == []


def test_a_second_stop_cannot_cut_the_cleanup_short(tmp_path):
    """Once stopping, further stops are ignored, so the cleanup that follows runs to its end."""
    done, _ = _stop_while_measuring(tmp_path, signal.SIGTERM)
    assert done.stdout.strip() == "True", done.stderr


#: A step the ratchet runs: says whether it has a session of its own, then waits to be stopped.
A_STEP = textwrap.dedent(
    """
    import os, pathlib, signal, sys
    fifo, marker = sys.argv[1], pathlib.Path(sys.argv[2])


    def stopped(*_):
        marker.write_text("stopped by SIGINT")
        sys.exit(0)


    signal.signal(signal.SIGINT, stopped)
    with open(fifo, "w") as told:
        told.write(f"{os.getpid()} {os.getsid(0) == os.getpid()}")
    while True:
        signal.pause()
    """
)

#: The ratchet, running that step; whether the step had finished when the ratchet ended.
RATCHET_IN_A_STEP = textwrap.dedent(
    """
    import importlib.util, pathlib, sys
    spec = importlib.util.spec_from_file_location("ratchet", sys.argv[1])
    ratchet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ratchet)
    ratchet.stop_cleanly()
    try:
        ratchet._run(sys.executable, "-c", sys.argv[2], sys.argv[3], sys.argv[4])
    finally:
        print(pathlib.Path(sys.argv[4]).exists(), flush=True)
    """
)


def _ratchet_stopped_in_a_step(tmp_path):
    """Stop the ratchet with SIGTERM while a step runs; a hang here is a test failure, not a run's."""
    fifo, marker = tmp_path / "told", tmp_path / "stopped"
    os.mkfifo(fifo)
    script = str(ROOT / "scripts/ratchet.py")
    with subprocess.Popen(
        [sys.executable, "-c", RATCHET_IN_A_STEP, script, A_STEP, str(fifo), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as ratchet:
        with fifo.open() as told:
            pid, own_session = told.read().split()
        try:
            ratchet.send_signal(signal.SIGTERM)
            out, err = ratchet.communicate(timeout=30)
        finally:
            for each in (int(pid), ratchet.pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(each, signal.SIGKILL)
    return own_session == "True", ratchet.returncode, out.strip(), marker, err


def test_a_stopped_quality_run_stops_its_step_and_waits_for_it(tmp_path):
    """A step left running would go on measuring, or mutating, after the ratchet had gone."""
    _, returncode, finished_first, marker, err = _ratchet_stopped_in_a_step(tmp_path)
    assert returncode == 128 + signal.SIGTERM, err
    assert marker.read_text() == "stopped by SIGINT"
    assert finished_first == "True"
    assert "kill -9 -- -" in err  # how to end it by hand, should it hang


def test_a_quality_step_hears_terminal_stops_only_through_the_ratchet(tmp_path):
    """In the terminal's group a Ctrl-C would reach the step twice: once itself, once from the ratchet."""
    own_session, *_ = _ratchet_stopped_in_a_step(tmp_path)
    assert own_session


def _package_found_at(monkeypatch, where, returncode=0):
    asked = []

    def run(*command, **_):
        asked.append(command)
        return subprocess.CompletedProcess(command, returncode, f"{where}\n", "No module named easee_mcp")

    monkeypatch.setattr(ratchet, "_run", run)
    return asked


def test_a_quality_run_refuses_a_package_from_another_checkout(monkeypatch, tmp_path):
    """Measured from another worktree's source, the levels would be that worktree's."""
    other = tmp_path / "other/src/easee_mcp/__init__.py"
    asked = _package_found_at(monkeypatch, other)
    with pytest.raises(SystemExit, match="uv sync") as refused:
        ratchet.check_the_package()
    assert str(other.parent) in str(refused.value)
    assert str(ratchet.SOURCE) in str(refused.value)
    assert asked == [("uv", "run", "python", "-c", "import easee_mcp; print(easee_mcp.__file__)")]


def test_a_quality_run_measures_a_package_from_its_own_checkout(monkeypatch):
    _package_found_at(monkeypatch, ratchet.PACKAGE / "__init__.py")
    ratchet.check_the_package()


def test_a_package_that_will_not_import_is_refused_too(monkeypatch):
    _package_found_at(monkeypatch, "", returncode=1)
    with pytest.raises(SystemExit, match="No module named easee_mcp"):
        ratchet.check_the_package()


def test_a_quality_run_checks_the_package_before_it_measures(monkeypatch):
    def refused():
        msg = "from another checkout"
        raise SystemExit(msg)

    ran = []
    monkeypatch.setattr(ratchet, "check_the_package", refused)
    monkeypatch.setattr(ratchet, "_run", lambda *c, **_: ran.append(c))
    with pytest.raises(SystemExit, match="another checkout"):
        ratchet.measure()
    assert ran == []
