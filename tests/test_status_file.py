"""The status file the voice bridge reads, in the shape every plugin shares."""

import ast
import asyncio
import datetime as dt
import json
import pathlib
import re

import pytest

from easee_mcp import status as status_module
from easee_mcp.adapter import EaseeReader
from easee_mcp.answers import ChargerAnswers
from easee_mcp.cache import CachedCharger
from easee_mcp.fake import FAKE_CHARGER_ID, FAKE_PASSWORD, FAKE_STREET, FAKE_USER, FakeEaseeCloud
from easee_mcp.server import build_server
from easee_mcp.status import StatusFile

AT = dt.datetime(2026, 11, 14, 12, 0, 10, tzinfo=dt.UTC)
SRC = pathlib.Path(__file__).parent.parent / "src"
#: The bridge's limits on what it shows, from the shared status design.
SAY_MAX, FIGURES_MAX, FIGURE_NAME = 120, 8, re.compile(r"[a-z_]{1,32}")
#: Calls that write, move or remove a file. ``replace`` and ``rename`` also
#: name string and date methods, so they count only when called on ``os``.
FILE_WRITES = frozenset(
    {
        "write_text",
        "write_bytes",
        "open",
        "unlink",
        "rmdir",
        "mkdir",
        "touch",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
        "copyfile",
        "copytree",
        "move",
        "rmtree",
        "dump",
    }
)
OS_WRITES = frozenset({"replace", "rename", "remove", "unlink", "makedirs", "mkdir", "write", "open"})


def _ask(tmp_path, *tools, cloud=None):
    cloud = cloud or FakeEaseeCloud(now=lambda: AT)
    reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=cloud.session())
    answers = ChargerAnswers(CachedCharger(reader, now=lambda: AT))
    status = StatusFile(tmp_path / "status.json", now=lambda: AT)
    server = build_server(answers, status=status)

    async def asked():
        for name, arguments in tools:
            await server.call_tool(name, arguments)

    asyncio.run(asked())
    return json.loads((tmp_path / "status.json").read_text())


def test_the_status_file_has_the_shape_every_plugin_shares(tmp_path):
    status = _ask(tmp_path, ("charger_status", {}))
    assert set(status) == {"at", "ok", "say", "figures"}
    assert status["at"] == AT.isoformat()
    assert isinstance(status["ok"], bool)
    assert len(status["say"]) <= SAY_MAX
    assert len(status["figures"]) <= FIGURES_MAX
    for name, value in status["figures"].items():
        assert FIGURE_NAME.fullmatch(name), name
        assert isinstance(value, int | float), name


def test_the_status_says_what_the_charger_does_and_the_calls_left(tmp_path):
    status = _ask(tmp_path, ("charger_status", {}), ("charger_sessions", {"days": 7}))
    assert status["ok"] is True
    assert status["say"] == "Garasje is charging at 7.2 kW; 9 of 10 session reads left this hour"
    assert status["figures"] == {"kw_now": 7.2, "session_kwh": 12.4, "calls_left_hour": 9}


def test_a_failed_read_shows_as_not_ok_with_its_sentence(tmp_path):
    status = _ask(tmp_path, ("charger_status", {}), cloud=FakeEaseeCloud(now=lambda: AT, retry_after=90))
    assert status["ok"] is False
    assert status["say"] == "Easee is limiting requests; ask again in 2 minutes."


def test_the_status_file_never_holds_the_id_the_place_or_session_times(tmp_path):
    cloud = FakeEaseeCloud(now=lambda: AT)
    cloud.sessions = [(AT.replace(hour=7, minute=13), None, 3.0)]
    _ask(tmp_path, ("charger_status", {}), ("charger_sessions", {"days": 7}), cloud=cloud)
    text = (tmp_path / "status.json").read_text()
    for private in (FAKE_CHARGER_ID, FAKE_STREET, "07:13", "latitude"):
        assert private not in text


def test_the_status_file_is_replaced_whole_never_written_in_place(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("old")
    before = path.stat().st_ino
    StatusFile(path, now=lambda: AT).record(ok=True, say="x", figures={})
    assert path.stat().st_ino != before  # a new file took its place
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]  # no temporary left


def test_the_status_file_is_written_only_when_it_changes(tmp_path):
    path = tmp_path / "status.json"
    status = StatusFile(path, now=lambda: AT)
    status.record(ok=True, say="x", figures={"kw_now": 1.0})
    first = path.stat().st_ino
    status.record(ok=True, say="x", figures={"kw_now": 1.0})
    assert path.stat().st_ino == first


def _writes_in(tree):
    """Every call in `tree` that writes a file."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in FILE_WRITES)
            or (isinstance(node.func, ast.Name) and node.func.id in FILE_WRITES)
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in OS_WRITES
            )
        )
    ]


#: The one place outside the status file that writes: the owner's own `easee-mcp login`,
#: run by hand, saving the login (ADR-EM-003), through the helper it writes each file with.
#: The server itself never calls it.
LOGIN_WRITER = ("cli.py", ("save_login", "_write_new"))


def test_the_status_file_is_the_only_file_the_server_writes():
    """Outside status.py, a write may stand only inside the function the login command saves with."""
    elsewhere = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        allowed = set()
        if path.name == LOGIN_WRITER[0]:
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in LOGIN_WRITER[1]:
                    allowed |= {id(n) for n in _writes_in(node)}
        if path.name != "status.py":
            elsewhere += [f"{path.name}:{n.lineno}" for n in _writes_in(tree) if id(n) not in allowed]
    assert elsewhere == []


def test_the_server_never_saves_the_login():
    """Serving and saying the status never reach the login writer; only the login verb does."""
    tree = ast.parse((SRC / "easee_mcp/cli.py").read_text())
    callers = {
        outer.name
        for outer in tree.body
        if isinstance(outer, ast.FunctionDef)
        for node in ast.walk(outer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "save_login"
    }
    helpers = {
        outer.name
        for outer in tree.body
        if isinstance(outer, ast.FunctionDef)
        for node in ast.walk(outer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_write_new"
    }
    assert callers == {"login"}
    assert helpers == {"save_login"}


def test_a_failed_status_write_leaves_no_temporary(tmp_path, monkeypatch):
    def broken(*_, **__):
        msg = "the disk is full"
        raise OSError(msg)

    monkeypatch.setattr(status_module.json, "dump", broken)
    with pytest.raises(OSError, match="the disk is full"):
        StatusFile(tmp_path / "status.json", now=lambda: AT).record(ok=True, say="x", figures={})
    assert list(tmp_path.iterdir()) == []


def test_a_status_write_that_cannot_open_its_temporary_closes_it(tmp_path, monkeypatch):
    def refused(*_, **__):
        msg = "no file objects left"
        raise OSError(msg)

    closed = []
    monkeypatch.setattr(status_module.os, "fdopen", refused)
    monkeypatch.setattr(status_module.os, "close", closed.append)
    with pytest.raises(OSError, match="no file objects left"):
        StatusFile(tmp_path / "status.json", now=lambda: AT).record(ok=True, say="x", figures={})
    assert len(closed) == 1
    assert list(tmp_path.iterdir()) == []
