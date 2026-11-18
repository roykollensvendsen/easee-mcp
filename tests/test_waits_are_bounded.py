"""A test that waits on another program fails when its own time is up, and leaves nothing running.

No limit is put on a test or a run from outside. Each wait inside a test carries its own bound,
so a child that never ends fails that one test instead of holding a mutation run for hours.
"""

import contextlib
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest
from bounded import WITHIN, line_within, run_bounded


def _running(pid):
    """Whether process `pid` still runs, rather than having ended or being a zombie left to reap."""
    try:
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def test_a_child_that_overruns_is_killed_with_everything_it_started(tmp_path):
    """A shell's background `sleep` holds the output open, and would outlive a plain timeout.

    The grandchild is checked by its own pid. The group or the shell alone could look gone
    while the sleep still runs, and that is the leftover this helper exists to end.
    """
    told = tmp_path / "pid"
    began = time.monotonic()
    try:
        with pytest.raises(pytest.fail.Exception, match=r"(?s)after 1 s.*started"):
            run_bounded(["sh", "-c", f'sleep 300 & echo $! > "{told}"; echo started; wait'], 1, text=True)
        assert time.monotonic() - began < 30  # the sleep holds the output, so an unkilled one would block
        grandchild = int(told.read_text())
        deadline = time.monotonic() + WITHIN
        while _running(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running(grandchild), "the shell's sleep was left running"
    finally:
        _end_if_still_the_sleep(told)


def _end_if_still_the_sleep(told):
    """End the grandchild by its own pid, and only while it is still the sleep this test started."""
    if not told.exists():
        return
    grandchild = int(told.read_text())
    with contextlib.suppress(OSError):
        if pathlib.Path(f"/proc/{grandchild}/cmdline").read_bytes().split(b"\0")[:2] == [b"sleep", b"300"]:
            os.kill(grandchild, signal.SIGKILL)


def test_a_child_that_ends_in_time_is_answered_like_subprocess_run():
    done = run_bounded(
        [sys.executable, "-c", "import sys; print(sys.stdin.read()); sys.exit(3)"], input="hello", text=True
    )
    assert (done.returncode, done.stdout) == (3, "hello\n")
    with pytest.raises(subprocess.CalledProcessError):
        run_bounded([sys.executable, "-c", "raise SystemExit(1)"], check=True)


def test_a_child_that_never_writes_fails_the_test_instead_of_hanging():
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE, text=True
    ) as child:
        began = time.monotonic()
        try:
            with pytest.raises(pytest.fail.Exception, match="no line within 1 s"):
                line_within(child.stdout, 1)
        finally:
            child.kill()
    assert time.monotonic() - began < 30


def test_two_lines_written_at_once_are_both_read():
    """The second line is already in the stream's buffer, where asking the pipe would not see it."""
    with subprocess.Popen(
        [sys.executable, "-c", "print('one\\ntwo', flush=True); import time; time.sleep(60)"],
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            assert line_within(child.stdout) == "one\n"
            assert line_within(child.stdout) == "two\n"
        finally:
            child.kill()


def test_a_group_gone_as_the_bound_runs_out_still_fails_cleanly(monkeypatch):
    """The group can end between the bound running out and the kill; that is still a clean failure."""
    real_killpg = os.killpg

    def gone_first(pid, sig):
        real_killpg(pid, sig)
        raise ProcessLookupError

    monkeypatch.setattr("bounded.os.killpg", gone_first)
    with pytest.raises(pytest.fail.Exception, match="was still running after"):
        run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], within=0.2)
