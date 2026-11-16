"""The MCP server as the voice sees it, and the command line, against the fake."""

import asyncio
import datetime as dt
import gc
import warnings

import pyeasee
import pytest
from mcp import Client

from easee_mcp import cli
from easee_mcp.adapter import EaseeReader, NotSetUpError
from easee_mcp.answers import ChargerAnswers
from easee_mcp.cache import CachedCharger
from easee_mcp.cli import main
from easee_mcp.fake import FAKE_CHARGER_ID, FAKE_PASSWORD, FAKE_USER, FakeEaseeCloud
from easee_mcp.server import FIRST_LINE_MAX, TOOLS, build_server

AT = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC)


def _server(cloud=None, *, simulated=False, login=lambda: (FAKE_USER, FAKE_PASSWORD), session=None):
    cloud = cloud or FakeEaseeCloud(now=lambda: AT)
    reader = EaseeReader(login, session=session or cloud.session())
    return build_server(ChargerAnswers(CachedCharger(reader, now=lambda: AT)), simulated=simulated)


def _tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def _call(server, name, arguments=None):
    return asyncio.run(server.call_tool(name, arguments or {})).structured_content


def test_the_voice_sees_four_tools_all_marked_read_only():
    tools = _tools(_server())
    assert (
        sorted(tools)
        == sorted(TOOLS)
        == ["charger_current", "charger_energy", "charger_sessions", "charger_status"]
    )
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


def test_each_tool_opens_with_a_whole_sentence_the_app_shows_uncut():
    for tool in _tools(_server()).values():
        first = tool.description.splitlines()[0]
        assert len(first) <= FIRST_LINE_MAX, tool.name
        assert first.endswith(("?", ".")), tool.name


def test_each_tool_answers_with_the_sentence_to_read_aloud():
    server = _server()
    assert _call(server, "charger_status")["say"].startswith("Garasje is charging at 7.2 kW")
    assert _call(server, "charger_current")["say"].startswith("Garasje may give the car up to 16 A")
    assert _call(server, "charger_sessions", {"days": 3})["say"] == "No charging sessions in the last 3 days."
    assert _call(server, "charger_energy", {"period": "month"})["say"] == "42.5 kWh charged this month."


def test_a_simulated_answer_says_so():
    server = _server(simulated=True)
    answer = _call(server, "charger_status")
    assert answer["say"].startswith("Simulated: ")
    assert answer["simulated"] is True
    assert server.instructions.startswith("SIMULATED")


class _Down:
    async def post(self, *_, **__):
        raise OSError

    async def close(self):
        pass


def _wrong_login():
    return FAKE_USER, "not-" + FAKE_PASSWORD


def _no_login():
    raise NotSetUpError


@pytest.mark.parametrize(
    ("kind", "say"),
    [
        ("down", "Easee's cloud isn't answering; the app or the button on the charger still works."),
        ("refused", "Easee refused the login. The saved username or password needs checking."),
        ("unset", "No Easee login is saved, so nothing was asked."),
        ("slow", "Easee is limiting requests; ask again in 3 minutes."),
    ],
)
def test_every_tool_answers_with_a_sentence_even_when_it_fails(kind, say):
    cloud = FakeEaseeCloud(now=lambda: AT, retry_after=150 if kind == "slow" else None)
    login = {"refused": _wrong_login, "unset": _no_login}.get(kind, lambda: (FAKE_USER, FAKE_PASSWORD))
    server = _server(cloud, login=login, session=_Down() if kind == "down" else None)
    answer = _call(server, "charger_status")
    assert answer["success"] is False
    assert answer["say"] == say


def test_the_instructions_say_it_only_reads():
    assert "never starts, stops or changes" in _server().instructions


def test_the_command_line_says_the_status_from_the_simulation(capsys):
    assert main(["status", "--simulate"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Simulated: Garasje is charging at 7.2 kW")
    assert FAKE_CHARGER_ID not in out


def test_the_status_command_closes_its_connection_to_easee(tmp_path, monkeypatch, capsys):
    """Even when Easee is down: left open, it shows on the owner's screen as an "Unclosed client session"."""
    folder = tmp_path / "login"
    folder.mkdir(mode=0o700)
    for name, value in (("user", FAKE_USER), ("password", FAKE_PASSWORD)):
        (folder / name).write_text(value)
        (folder / name).chmod(0o600)
    monkeypatch.setattr(cli, "STATUS_FILE", tmp_path / "status.json")
    started = pyeasee.Easee.__init__

    def nowhere(self, *args, **kwargs):
        """A real connection, sent to a closed local port, so Easee is down at once and nothing leaves."""
        started(self, *args, **kwargs)
        self.base = "http://127.0.0.1:1"

    monkeypatch.setattr(pyeasee.Easee, "__init__", nowhere)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert main(["status", "--login-dir", str(folder)]) == 0
        gc.collect()
    assert "isn't answering" in capsys.readouterr().out
    assert not [warning.message for warning in seen if "Unclosed" in str(warning.message)]


def test_the_voice_server_closes_its_connection_to_easee_when_it_stops(monkeypatch):
    """The voice system stops the server; the connection it read with must not outlive it."""
    started = pyeasee.Easee.__init__
    opened = []

    def nowhere(self, *args, **kwargs):
        """A real connection, sent to a closed local port, so Easee is down at once and nothing leaves."""
        started(self, *args, **kwargs)
        self.base = "http://127.0.0.1:1"
        opened.append(self.session)

    monkeypatch.setattr(pyeasee.Easee, "__init__", nowhere)
    reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD))
    server = build_server(ChargerAnswers(CachedCharger(reader, now=lambda: AT)))

    async def one_question():
        async with Client(server) as client:
            return await client.call_tool("charger_status", {})

    answer = asyncio.run(one_question())
    assert "isn't answering" in answer.structured_content["say"]
    assert opened
    assert all(session.closed for session in opened)
