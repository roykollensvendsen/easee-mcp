"""The mutation runner's own rules, each with a test named after it.

`scripts/mutate.py` proves every other rule, so its own rules are proven the same
way: `scripts/mutations/scripts/mutate.py/` switches each off in turn, and the test
here that names it goes red. The runner has already loaded its code when it switches
a line off, and the suite it starts loads the switched-off file, so it can prove itself.
The way is garage-door's.

None of these runs a real suite: the suite is a stand-in that records what it was
asked to do, so a test here never mutates anything.
"""

import importlib.util
import pathlib
import subprocess

import pytest

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
