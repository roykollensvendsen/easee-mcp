"""Checks that hold over the whole test session, not one test.

Each run keeps every temporary file its tests make in one folder of its own: pytest's
folders, anything made with `tempfile`, and anything a program the tests start makes
under $TMPDIR. The run holds a lock on that folder while it lasts, and removes the folder
when it ends. A killed run never gets that far, so the next run removes any such folder
whose lock nobody holds. The lock, not a process number, tells a live run: a sandbox has
process numbers of its own, and they are reused.

pytest's own sweep is not used for this: it works on a folder every project on the machine
shares, and set to keep nothing it removes folders other runs are still using. Proven by
`tests/test_no_temp_left.py`.

The run also fails if a test changed the rule check's real recovery file.
"""

import fcntl
import os
import pathlib
import re
import secrets
import shutil
import stat
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
RECOVERY = ROOT / "scripts/.mutate-recovery.json"
BEFORE = pytest.StashKey[bytes | None]()
#: This run's folder, the lock it holds on it, and $TMPDIR and tempfile's folder from before.
RUN = pytest.StashKey[tuple[pathlib.Path, int, str | None, str | None]]()
PREFIX = "easee-mcp-tests-"
NAMED = re.compile(re.escape(PREFIX) + r"\d+")
LOCK = ".lock"
OPEN_LOCK = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW


def _new_run_folder(temp: pathlib.Path) -> pathlib.Path:
    while True:
        folder = temp / f"{PREFIX}{secrets.randbelow(10**12):012d}"
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return folder


def sweep_dead_runs(temp: pathlib.Path) -> None:
    """Remove the run folders in `temp` whose lock nobody holds: the folders of runs that died.

    Only a real folder named like a run's, and owned by this user, is ever looked at; a link
    is never followed.
    """
    for path in temp.iterdir() if temp.is_dir() else []:
        if not NAMED.fullmatch(path.name):
            continue
        try:
            found = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
            continue
        try:
            lock = os.open(path / LOCK, OPEN_LOCK, 0o600)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # a run still going holds it
            shutil.rmtree(path, ignore_errors=True)
        finally:
            os.close(lock)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp:  # a run told where to keep them keeps them there
        return
    before, cached = os.environ.get("TMPDIR"), tempfile.tempdir  # gettempdir() fills tempdir in
    temp = pathlib.Path(tempfile.gettempdir())
    sweep_dead_runs(temp)
    folder = _new_run_folder(temp)
    lock = os.open(folder / LOCK, OPEN_LOCK, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    config.stash[RUN] = (folder, lock, before, cached)
    os.environ["TMPDIR"] = str(folder)
    tempfile.tempdir = str(folder)
    # A folder inside it: pytest empties its basetemp first, which would take the lock too.
    config.option.basetemp = str(folder / "pytest")


def pytest_unconfigure(config: pytest.Config) -> None:
    run = config.stash.get(RUN, None)
    if run is None:
        return
    folder, lock, before, cached = run
    if before is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = before
    tempfile.tempdir = cached
    shutil.rmtree(folder, ignore_errors=True)
    os.close(lock)


def _now() -> bytes | None:
    return RECOVERY.read_bytes() if RECOVERY.is_file() else None


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[BEFORE] = _now()


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _now() != session.config.stash[BEFORE]:
        print(  # noqa: T201
            "\nA test changed scripts/.mutate-recovery.json, the rule check's real recovery file. "
            "A test that runs scripts/mutate.py must point RECOVERY at its own temporary file."
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
