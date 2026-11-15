"""Each test leaves the stop handlers as it found them.

`scripts/mutate.py` and `scripts/ratchet.py` set handlers for SIGINT, SIGTERM and SIGHUP
when their `main()` runs, and a test that runs one in-process would leave them set for
every test after it. `tests/conftest.py` saves them before each test and puts them back
after. Proven here in a scratch run guarded by this repository's conftest.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent.parent

TWO_TESTS = """
import signal

STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
AT_START = {s: signal.getsignal(s) for s in STOPS}


def test_a_changes_them():
    for each in STOPS:
        signal.signal(each, signal.SIG_IGN)


def test_b_finds_them_as_they_were():
    assert {s: signal.getsignal(s) for s in STOPS} == AT_START
"""


def test_tests_leave_the_stop_handlers_as_they_found_them(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").write_text((ROOT / "tests/conftest.py").read_text(encoding="utf-8"))
    (tmp_path / "tests/test_scratch.py").write_text(TWO_TESTS)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout
    assert "2 passed" in done.stdout
