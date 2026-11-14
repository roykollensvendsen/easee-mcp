#!/usr/bin/env python3
"""Refuse a commit that would publish where the owner lives, their charger's ID, or their login.

This repository is public. Nothing in it may name the owner's street or town,
carry the ID of their real charger, or hold their Easee login. The checks, on
every file given (by default the files staged for commit), or on the whole
history:

- no home-network address: a private IPv4 address (examples use 192.0.2.x);
- no street address, such as a Norwegian street name followed by a number;
- no coordinates more exact than two decimals;
- no string shaped like an Easee charger ID, except the made-up ``EHTEST..``;
- no word from the owner's private list, such as a charger's own name;
- not the owner's Easee username or password.

The private list is ``~/.config/easee-mcp/private.toml``, holding
``words = ["...", ...]``. It lives outside the repository, so the list itself is
never published. The login is checked only when ``--secret-file`` names its
files, so the commit hook never reads it; the owner runs that check once before
a push.

What is found is reported by kind and line, never quoted, since the report may
land in a public log.

    python3 scripts/private_data_scan.py            # the staged files (pre-commit)
    python3 scripts/private_data_scan.py --tracked  # every tracked file
    python3 scripts/private_data_scan.py --history \
        --secret-file ~/.config/easee-mcp/user --secret-file ~/.config/easee-mcp/password
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORDS = pathlib.Path("~/.config/easee-mcp/private.toml")
#: Shorter words would match ordinary text; shorter secrets are not secrets.
MIN_WORD = 4
MIN_SECRET = 6

PRIVATE_IPV4 = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])"
)
STREET = re.compile(
    r"\b[A-ZÆØÅ][a-zæøå]+(?:gata|gaten|gate|veien|vegen|vei|veg|torget|plassen|allé|alleen)"
    r"\s+\d{1,4}[A-Za-z]?\b"
)
#: Norway lies within about 57-72 north and 4-32 east.
COORDINATES = re.compile(r"\b(?:5[7-9]|6\d|7[0-2])\.\d{3,}\s*,\s*(?:[4-9]|[12]\d|3[0-2])\.\d{3,}\b")
#: An Easee charger ID: E, a product letter, then six capitals or digits with at
#: least one digit among them, so an ordinary capitalised word is not one.
CHARGER_ID = re.compile(r"\bE[A-Z](?=[A-Z0-9]{0,5}\d)[A-Z0-9]{6}\b")
#: The made-up IDs the fake and the tests use.
MADE_UP_ID = re.compile(r"EHTEST\d\d")


def private_values(words: pathlib.Path, secrets: list[pathlib.Path]) -> set[str]:
    """The owner's private words, and their login, from files outside the repository."""
    out: set[str] = set()
    if words.is_file():
        listed = tomllib.loads(words.read_text()).get("words", [])
        out |= {w for w in listed if isinstance(w, str) and len(w) >= MIN_WORD}
    for path in secrets:
        secret = path.read_text().strip() if path.is_file() else ""
        if len(secret) >= MIN_SECRET:
            out.add(secret)
    return out


def offences(text: str, private: set[str]) -> list[str]:
    """What in ``text`` would say where the owner lives, or name their charger, by kind only."""
    found = [f"line {_line(text, m.start())}: a home-network address" for m in PRIVATE_IPV4.finditer(text)]
    found += [f"line {_line(text, m.start())}: a street address" for m in STREET.finditer(text)]
    found += [f"line {_line(text, m.start())}: exact coordinates" for m in COORDINATES.finditer(text)]
    found += [
        f"line {_line(text, m.start())}: a charger ID"
        for m in CHARGER_ID.finditer(text)
        if not MADE_UP_ID.fullmatch(m.group())
    ]
    lowered = text.lower()
    for value in private:
        at = lowered.find(value.lower())
        if at >= 0:
            found.append(f"line {_line(text, at)}: a private word or the login")
    return found


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return done.stdout


def _texts(*, tracked: bool, history: bool) -> list[tuple[str, str]]:
    """Each place to scan, as (what it is, its text)."""
    if history:
        return [("the history", _git("log", "-p", "--all", "--no-color", "--format=commit %H%n%B"))]
    names = _git("ls-files") if tracked else _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    out = []
    for name in names.splitlines():
        path = ROOT / name
        try:
            out.append((name, path.read_text()))
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    """Scan the staged files, every tracked file, or the history; 1 when anything is found."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tracked", action="store_true", help="scan every tracked file")
    parser.add_argument("--history", action="store_true", help="scan every commit on every branch")
    parser.add_argument("--words", default=str(WORDS), help=f"the private list (default {WORDS})")
    parser.add_argument(
        "--secret-file",
        action="append",
        default=[],
        help="also refuse this file's text (by default none is read)",
    )
    args = parser.parse_args(argv)
    secrets = [pathlib.Path(p).expanduser() for p in args.secret_file]
    private = private_values(pathlib.Path(args.words).expanduser(), secrets)
    bad = 0
    for where, text in _texts(tracked=args.tracked, history=args.history):
        for offence in offences(text, private):
            sys.stderr.write(f"{where}, {offence}\n")
            bad += 1
    if bad:
        sys.stderr.write("Use made-up names, EHTEST01, 192.0.2.x, and two decimals at most in examples.\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
