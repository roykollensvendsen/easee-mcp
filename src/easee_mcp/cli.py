"""Command line for easee-mcp: serve the charger tools, or say the charger's status once."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import pathlib
import sys

from easee_mcp.adapter import EaseeReader, NotSetUpError
from easee_mcp.answers import ChargerAnswers
from easee_mcp.cache import CachedCharger
from easee_mcp.fake import FAKE_PASSWORD, FAKE_USER, FakeEaseeCloud
from easee_mcp.status import STATUS_FILE, StatusFile

#: Every verb this command has. The documentation test requires each one to
#: appear in a worked example, so a verb cannot be added without one.
COMMANDS: tuple[str, ...] = ("serve", "status")
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
        password = (folder / "password").read_text().strip()
    except OSError:
        raise NotSetUpError from None
    if not user or not password:
        raise NotSetUpError
    return user, password


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
    for verb in (serve, status):
        verb.add_argument("--simulate", action="store_true", help="answer from a made-up charger")
        verb.add_argument(
            "--login-dir", help=f"the folder holding 'user' and 'password' (default {LOGIN_DIR})"
        )
    args = parser.parse_args(argv)

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
