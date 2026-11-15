"""Command line for easee-mcp: serve the charger tools, say the charger's status once, or save the login."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import functools
import getpass
import os
import pathlib
import secrets
import signal
import sys

from easee_mcp.adapter import EaseeReader, NotSetUpError
from easee_mcp.answers import ChargerAnswers
from easee_mcp.cache import CachedCharger
from easee_mcp.fake import FAKE_PASSWORD, FAKE_USER, FakeEaseeCloud
from easee_mcp.status import STATUS_FILE, StatusFile

#: Every verb this command has. The documentation test requires each one to
#: appear in a worked example, so a verb cannot be added without one.
COMMANDS: tuple[str, ...] = ("serve", "status", "login")
#: Where the owner's Easee login is kept, readable by them alone. It is read
#: only when a read is asked for, and never printed or logged.
LOGIN_DIR = pathlib.Path("~/.config/easee-mcp")
#: Help is laid out at one width on every terminal, so the README's copy of it stays true.
HELP = functools.partial(argparse.HelpFormatter, width=100)


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def saved_login(folder: pathlib.Path) -> tuple[str, str]:
    """The username and password saved in ``folder``; NotSetUpError when either is missing."""
    try:
        user = (folder / "user").read_text().strip()
        # Spaces at either end belong to a password; only the file's own line end goes.
        password = (folder / "password").read_text().removesuffix("\n")
    except OSError:
        raise NotSetUpError from None
    if not user or not password:
        raise NotSetUpError
    return user, password


class NotYoursError(Exception):
    """The login folder is a link, or not a folder of this user's."""


#: The stops held off while the two files are put in place, so they always change together.
STOPS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def save_login(folder: pathlib.Path, user: str, password: str) -> None:
    """Save the login in ``folder``, readable by its owner alone, both files or neither.

    The folder is opened once, refusing a link, and checked as this user's; every write
    then goes through that open folder, so it cannot be swapped for a link on the way.
    """
    folder.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=0o700)
    try:
        held = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        msg = f"{folder} is a link, or not a folder of yours: nothing was saved."
        raise NotYoursError(msg) from error
    written: list[tuple[str, str]] = []
    try:
        _check_ours(held, folder)
        # RULE: the login folder is readable by its owner alone
        os.fchmod(held, 0o700)
        for name, value in (("password", password), ("user", user)):
            written.append((_write_new(held, name, value), name))
        before = signal.pthread_sigmask(signal.SIG_BLOCK, STOPS)
        try:
            for temporary, name in written:
                os.replace(temporary, name, src_dir_fd=held, dst_dir_fd=held)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, before)
    except BaseException:
        for temporary, _ in written:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=held)
        raise
    finally:
        os.close(held)


def _check_ours(held: int, folder: pathlib.Path) -> None:
    found = os.fstat(held)
    if found.st_uid != os.getuid():
        not_yours = f"{folder} is not a folder of yours: nothing was saved."
        # RULE: a login folder that is not yours is refused
        raise NotYoursError(not_yours)
    if found.st_mode & 0o022:
        # Someone else may already have put a file there; closing it now would hide that.
        open_to_others = (
            f"Others could write in {folder}, so nothing was saved. Look inside it, then run"
            f" `chmod 700 {folder}` and log in again."
        )
        # RULE: a login folder others could write in is refused
        raise NotYoursError(open_to_others)


def _write_new(folder: int, name: str, value: str) -> str:
    """Write ``value`` to a new file of its own in the open ``folder``, made 0600; its name."""
    temporary = f".{name}-{secrets.token_hex(8)}"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(value + "\n")
    except BaseException:
        # RULE: a failed save leaves no copy of the login
        os.unlink(temporary, dir_fd=folder)
        raise
    return temporary


def login(folder: pathlib.Path) -> int:
    """Ask for the Easee login without showing it, and save it. Nothing is sent to Easee."""
    user = getpass.getpass("Easee username (not shown): ").strip()
    password = getpass.getpass("Easee password (not shown): ")  # kept as typed
    if not user or not password:
        sys.stderr.write("An empty answer: nothing was saved, and any login saved before is kept.\n")
        # RULE: an empty login is never saved
        return 1
    try:
        save_login(folder, user, password)
    except NotYoursError as refused:
        sys.stderr.write(f"{refused}\n")
        return 1
    sys.stdout.write(f"Saved in {folder}, readable by you alone. Check it with: easee-mcp status\n")
    return 0


def _answers(*, simulate: bool, login_dir: str | None) -> ChargerAnswers:
    if simulate:
        # Made-up charger, made-up login: nothing is sent and nothing is left behind.
        reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=FakeEaseeCloud().session())
    else:
        folder = pathlib.Path(login_dir).expanduser() if login_dir else LOGIN_DIR.expanduser()
        reader = EaseeReader(functools.partial(saved_login, folder))
    return ChargerAnswers(CachedCharger(reader, now=_now))


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="easee-mcp", description=__doc__, formatter_class=HELP)
    verbs = parser.add_subparsers(dest="command", required=True)
    serve = verbs.add_parser(
        formatter_class=HELP, name="serve", help="serve the charger tools over stdio, for a voice session"
    )
    status = verbs.add_parser(
        formatter_class=HELP, name="status", help="say what the charger is doing, as the voice would hear it"
    )
    saving = verbs.add_parser(
        formatter_class=HELP, name="login", help="ask for the Easee login without showing it, and save it"
    )
    for verb in (serve, status):
        verb.add_argument("--simulate", action="store_true", help="answer from a made-up charger")
    for verb in (serve, status, saving):
        verb.add_argument(
            "--login-dir", help=f"the folder holding 'user' and 'password' (default {LOGIN_DIR})"
        )
    args = parser.parse_args(argv)
    if args.command == "login":
        return login(pathlib.Path(args.login_dir).expanduser() if args.login_dir else LOGIN_DIR.expanduser())

    # The MCP server is a slow import, and only a working command needs it.
    from mcp.types import CallToolResult  # noqa: PLC0415

    from easee_mcp.server import build_server  # noqa: PLC0415

    # The simulation leaves nothing behind, so it writes no status file.
    status_file = None if args.simulate else StatusFile(STATUS_FILE.expanduser(), now=_now)
    answers = _answers(simulate=args.simulate, login_dir=args.login_dir)
    server = build_server(answers, simulated=args.simulate, status=status_file)
    if args.command == "status":
        result = asyncio.run(server.call_tool("charger_status", {}))
        answer = result.structured_content if isinstance(result, CallToolResult) else None
        sys.stdout.write(str((answer or {}).get("say", "")) + "\n")
        return 0
    server.run("stdio")  # pragma: no cover - runs until stdin closes
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
