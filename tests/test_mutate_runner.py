"""The mutation runner's own rules, each with a test named after it.

`scripts/mutate.py` proves every other rule, so its own rules are proven the same
way: `scripts/mutations/scripts/mutate.py/` switches each off in turn, and the test
here that names it goes red. The runner has already loaded its code when it switches
a line off, and the suite it starts loads the switched-off file, so it can prove itself.
The way is garage-door's.

None of these runs a real suite: the suite is a stand-in that records what it was
asked to do, so a test here never mutates anything.
"""

import fcntl
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import types

import pytest
from bounded import WITHIN, line_within

ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture
def mutate():
    """The runner, loaded by path: scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("mutate_runner", ROOT / "scripts/mutate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _suite_says(monkeypatch, mutate, stdout, returncode=1):
    """Stand in for the suite: it answers `stdout`, and the arguments it got are kept."""
    asked = {}

    def run(args, **kwargs):
        asked.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    monkeypatch.setattr(mutate.subprocess, "run", run)
    return asked


def test_a_file_changed_during_the_run_is_never_restored_over(tmp_path, mutate):
    """Someone saved the file while the suite ran: putting the original back would lose their edit."""
    source = tmp_path / "cache.py"
    source.write_text("edited while the suite ran\n")
    assert mutate.restore(source, "original\n", "mutated\n") is False
    assert source.read_text() == "edited while the suite ran\n"


def test_two_mutations_never_share_a_bytecode_cache(monkeypatch, mutate):
    """Two equal-size mutations in one second would otherwise import the last one's bytecode."""
    # Unset here, so the runner has to set it itself: a mutation run sets it for the suite it starts.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    asked = _suite_says(monkeypatch, mutate, "1 passed\n", returncode=0)
    mutate.run_suite()
    assert asked["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_real_mutation_run_puts_no_time_limit_on_the_suite(monkeypatch, mutate):
    """A slow suite is measured to its end; only a test's scratch run is given a bound."""
    asked = _suite_says(monkeypatch, mutate, "1 passed\n", returncode=0)
    mutate.run_suite()
    assert asked["timeout"] is None
    mutate.run_suite(timeout=WITHIN)
    assert asked["timeout"] == WITHIN


def test_a_coloured_failure_line_is_still_attributed(monkeypatch, mutate):
    """With colour on, FAILED is wrapped in codes, and a kill must still name its test."""
    coloured = (
        "\x1b[31mFAILED\x1b[0m tests/test_cache.py::test_a_429_stops_calls_until_retry_after_passes"
        " - assert\n"
        "\x1b[31m1 failed\x1b[0m\n"
    )
    _suite_says(monkeypatch, mutate, coloured)
    assert mutate.run_suite() == (1, ["test_a_429_stops_calls_until_retry_after_passes"])


def _repository(tmp_path, *, marked):
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "scripts/mutations").mkdir(parents=True)
    (tmp_path / "src/pkg/core.py").write_text("# RULE: a rule\n" if marked else "x = 1\n")
    return tmp_path


def test_rules_marked_with_no_rows_fail_the_run(tmp_path, monkeypatch, mutate, capsys):
    """A rows folder emptied by mistake would leave every rule unproven and the run green."""
    monkeypatch.setattr(mutate.sys, "argv", ["mutate.py"])
    root = _repository(tmp_path, marked=True)
    monkeypatch.setattr(mutate, "ROOT", root)
    monkeypatch.setattr(mutate, "ROWS", root / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", root / "scripts/.mutate-recovery.json")
    assert mutate.main() == 1
    assert "src/pkg/core.py mark rules" in capsys.readouterr().out


def test_a_repository_with_no_rules_marked_passes_with_no_rows(tmp_path, monkeypatch, mutate):
    """A repository just scaffolded has nothing to prove yet."""
    monkeypatch.setattr(mutate.sys, "argv", ["mutate.py"])
    root = _repository(tmp_path, marked=False)
    monkeypatch.setattr(mutate, "ROOT", root)
    monkeypatch.setattr(mutate, "ROWS", root / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", root / "scripts/.mutate-recovery.json")
    assert mutate.main() == 0


#: The one function whose rules cannot be proven: a killed run repairs with the code it left
#: switched off. The way is garage-door's.
UNPROVABLE = "repair"

REPAIR_WITH_A_RULE = '''
def repair():
    """Put back a file a killed run left mutated."""
    # RULE: a repaired file is the original
    if kept:
        write(kept)


def sweep():
    # RULE: a swept folder is gone
    remove()
'''


def _inside_repair(text, rows, mutate):
    """The rules marked, or switched off by a row, in `repair()` or any function it calls by name."""
    import ast  # noqa: PLC0415

    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    functions = {f.name: f for f in ast.parse(text).body if isinstance(f, defs)}
    assert UNPROVABLE in functions, (
        f"no {UNPROVABLE}() in the runner; if it was renamed, rename UNPROVABLE too"
    )
    reached, waiting = set(), [UNPROVABLE]
    while waiting:  # repair() and everything it calls, however deep, within this file
        name = waiting.pop()
        reached.add(name)
        calls = [
            c for c in ast.walk(functions[name]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        called = {c.func.id for c in calls}
        waiting += [c for c in called if c in functions and c not in reached]
    lines = text.splitlines()
    inside = {i for name in reached for i in range(functions[name].lineno - 1, functions[name].end_lineno)}
    marked = [lines[i].split("# RULE:")[1].strip() for i in sorted(inside) if "# RULE:" in lines[i]]
    rowed = [
        r["name"] for r in rows if mutate.line_of(text, r["find"], int(r.get("occurrence", 1))) in inside
    ]
    return sorted(set(marked + rowed))


def test_a_rule_inside_repair_is_found(mutate):
    """The guard below is only worth something if it sees a rule where one has been put."""
    rows = [
        {"name": "a swept folder is gone", "find": r"^\s*remove\(\)$"},
        {"name": "kept", "find": r"if kept:"},
    ]
    assert _inside_repair(REPAIR_WITH_A_RULE, rows, mutate) == ["a repaired file is the original", "kept"]


def test_no_rule_lives_inside_repair(mutate):
    """A killed run repairs with the code it left switched off, so a rule in repair() cannot be proven."""
    text = (ROOT / "scripts/mutate.py").read_text(encoding="utf-8")
    rows = [r for r in mutate.all_rows() if r["file"] == "scripts/mutate.py"]
    assert rows, "the runner's own rows are missing"
    assert _inside_repair(text, rows, mutate) == []


REPAIR_CALLING_A_RULE = """
def write(kept):
    # RULE: a repaired file is written whole
    path.write_text(kept)


def repair():
    write(kept)
"""


def test_a_rule_in_what_repair_calls_is_found(mutate):
    """A helper repair() calls runs switched off too, so its rule cannot be proven either."""
    assert _inside_repair(REPAIR_CALLING_A_RULE, [], mutate) == ["a repaired file is written whole"]


def test_a_runner_with_no_repair_is_refused_by_name(mutate):
    """Renamed, repair() would leave the guard checking nothing; it says what it looked for."""
    with pytest.raises(AssertionError, match=r"no repair\(\) in"):
        _inside_repair("def mend():\n    pass\n", [], mutate)


REPAIR_AWAITING_A_RULE = """
async def write(kept):
    # RULE: an awaited repair is written whole
    path.write_text(kept)


def repair():
    run(write(kept))
"""


def test_a_rule_in_an_async_helper_repair_calls_is_found(mutate):
    """An `async def` helper runs switched off too, once repair() calls it."""
    assert _inside_repair(REPAIR_AWAITING_A_RULE, [], mutate) == ["an awaited repair is written whole"]


def _one_rule(tmp_path, *, left_mutated=False):
    """A scratch checkout with one rule's row; optionally as a killed run left it."""
    root = _repository(tmp_path, marked=False)
    source = root / "src/pkg/core.py"
    source.write_text("x = 2\n" if left_mutated else "x = 1\n")
    rows = root / "scripts/mutations/src/pkg/core.py"
    rows.mkdir(parents=True)
    (rows / "a-rule.toml").write_text(
        '[[rule]]\nname = "a rule"\nfile = "src/pkg/core.py"\nfind = "x = 1"\nreplace = "x = 2"\n'
    )
    if left_mutated:
        kept = {"file": "src/pkg/core.py", "original": "x = 1\n", "mutated": "x = 2\n"}
        (root / "scripts/.mutate-recovery.json").write_text(json.dumps(kept))
    return root, source


def _point_at(monkeypatch, mutate, root, *argv):
    monkeypatch.setattr(mutate.sys, "argv", ["mutate.py", *argv])
    monkeypatch.setattr(mutate, "ROOT", root)
    monkeypatch.setattr(mutate, "ROWS", root / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", root / "scripts/.mutate-recovery.json")
    suites = []
    monkeypatch.setattr(mutate, "run_suite", lambda: suites.append(1) or (1, ["test_a_rule"]))
    return suites


def test_one_mutation_run_per_checkout(tmp_path, monkeypatch, mutate):
    """A second run would repair the first one's live mutation, and both would measure nonsense."""
    root, source = _one_rule(tmp_path, left_mutated=True)
    suites = _point_at(monkeypatch, mutate, root)
    first = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="already running"):
            mutate.main()
    finally:
        os.close(first)
    assert source.read_text() == "x = 2\n"  # the first run's mutation, untouched
    assert (root / "scripts/.mutate-recovery.json").exists()
    assert suites == []


def test_a_mutation_run_names_each_rule_before_it_tests_it(tmp_path, monkeypatch, mutate):
    """The name must be out of the process, not in its buffer, by the time the suite starts.

    In CI the output is a pipe, which Python holds back until it is full; a run that hung
    on one rule for six hours left nothing in the log to say which.
    """

    class Pipe:
        """Output that, like a pipe, shows only what has been flushed."""

        def __init__(self):
            self.held, self.shown = "", ""

        def write(self, text):
            self.held += text
            return len(text)

        def flush(self):
            self.shown, self.held = self.shown + self.held, ""

    root, _ = _one_rule(tmp_path)
    _point_at(monkeypatch, mutate, root)
    monkeypatch.setattr(mutate, "check_the_package", lambda: None)
    pipe = Pipe()
    monkeypatch.setattr(mutate.sys, "stdout", pipe)
    seen = []
    monkeypatch.setattr(mutate, "run_suite", lambda: seen.append(pipe.shown) or (1, ["test_a_rule"]))
    mutate.main()
    assert "a rule" in seen[0]


def test_a_mutation_run_says_how_long_each_rule_took(tmp_path, monkeypatch, mutate, capsys):
    """Each result line ends with the seconds its suite took, so a slow rule stands out."""
    root, _ = _one_rule(tmp_path)
    _point_at(monkeypatch, mutate, root)
    monkeypatch.setattr(mutate, "check_the_package", lambda: None)
    clock = iter([10.0, 52.4])
    monkeypatch.setattr(mutate, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    mutate.main()
    assert "KILLED   a rule: 1 in 42 s\n" in capsys.readouterr().out


def test_a_repair_only_run_only_repairs(tmp_path, monkeypatch, mutate):
    root, source = _one_rule(tmp_path, left_mutated=True)
    suites = _point_at(monkeypatch, mutate, root, "--repair-only")
    assert mutate.main() == 0
    assert source.read_text() == "x = 1\n"
    assert not (root / "scripts/.mutate-recovery.json").exists()
    assert suites == []


STOPPED_MID_MUTATION = textwrap.dedent(
    """
    import importlib.util, pathlib, sys
    spec = importlib.util.spec_from_file_location("mutate", sys.argv[1])
    mutate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutate)
    root = pathlib.Path(sys.argv[2])
    mutate.ROOT, mutate.ROWS = root, root / "scripts/mutations"
    mutate.RECOVERY = root / "scripts/.mutate-recovery.json"
    put_back = mutate.restore


    def measuring():
        print("measuring", flush=True)
        sys.stdin.readline()
        return 1, ["test_a_rule"]


    def slowed(*args):
        print("cleaning", flush=True)
        sys.stdin.readline()
        return put_back(*args)


    mutate.run_suite, mutate.restore = measuring, slowed
    mutate.check_the_package = lambda: None
    sys.argv = ["mutate.py"]
    sys.exit(mutate.main())
    """
)


def _stop_mid_mutation(tmp_path, *stops):
    """Start a run, stop it while it measures, and send `stops[1:]` once its cleanup has begun."""
    root, source = _one_rule(tmp_path)
    with subprocess.Popen(
        [sys.executable, "-c", STOPPED_MID_MUTATION, str(ROOT / "scripts/mutate.py"), str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as run:
        try:
            assert line_within(run.stdout) == "testing  a rule\n"
            assert line_within(run.stdout) == "measuring\n"
            assert source.read_text() == "x = 2\n"
            run.send_signal(stops[0])
            began = line_within(run.stdout)
            for each in stops[1:]:
                run.send_signal(each)
            _, err = run.communicate("\n", timeout=WITHIN)
        finally:
            if run.poll() is None:  # a run that never answered; leaving the block would wait on it
                run.kill()
    return began, source.read_text(), (root / "scripts/.mutate-recovery.json").exists(), err


@pytest.mark.parametrize(
    "signum",
    [signal.SIGINT, signal.SIGTERM, signal.SIGHUP],
    ids=["interrupted", "cancelled", "hung up"],
)
def test_a_stopped_mutation_run_puts_the_source_back(tmp_path, signum):
    """Ctrl-C, a cancelled CI job and a closed terminal each leave the source as it was."""
    began, text, recovery_left, err = _stop_mid_mutation(tmp_path, signum)
    assert began == "cleaning\n", err
    assert text == "x = 1\n"
    assert not recovery_left


def test_a_second_stop_cannot_cut_a_mutation_runs_cleanup_short(tmp_path):
    """Ctrl-C, then a SIGTERM while the source is being put back: it is still put back."""
    began, text, recovery_left, err = _stop_mid_mutation(tmp_path, signal.SIGINT, signal.SIGTERM)
    assert began == "cleaning\n", err
    assert text == "x = 1\n"
    assert not recovery_left


def _two_checkouts(tmp_path):
    """Two checkouts, each holding a package of the same name."""
    here, other = tmp_path / "here", tmp_path / "other"
    for root in (here, other):
        (root / "src/pkg").mkdir(parents=True)
        (root / "src/pkg/__init__.py").write_text("")
    return here, other


def test_a_mutation_run_refuses_a_package_from_another_checkout(tmp_path, monkeypatch, mutate):
    """A venv shared with another worktree measures that worktree's code, and every rule looks the same."""
    here, other = _two_checkouts(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", here)
    monkeypatch.setenv("PYTHONPATH", str(other / "src"))
    with pytest.raises(SystemExit, match="uv sync") as refused:
        mutate.check_the_package()
    assert str(other / "src/pkg") in str(refused.value)
    assert str(here / "src") in str(refused.value)


def test_a_package_from_this_checkout_is_measured(tmp_path, monkeypatch, mutate):
    here, _ = _two_checkouts(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", here)
    monkeypatch.setenv("PYTHONPATH", str(here / "src"))
    mutate.check_the_package()


def test_a_mutation_run_checks_the_package_before_it_measures(tmp_path, monkeypatch, mutate):
    root, source = _one_rule(tmp_path)
    suites = _point_at(monkeypatch, mutate, root)

    def refused():
        msg = "from another checkout"
        raise SystemExit(msg)

    monkeypatch.setattr(mutate, "check_the_package", refused)
    with pytest.raises(SystemExit, match="another checkout"):
        mutate.main()
    assert suites == []
    assert source.read_text() == "x = 1\n"


def test_a_mutation_run_refuses_a_checkout_with_no_package(tmp_path, monkeypatch, mutate):
    """Run from the wrong folder, the check would find nothing to check and let the run go on."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="no package") as refused:
        mutate.check_the_package()
    assert str(tmp_path / "src") in str(refused.value)
