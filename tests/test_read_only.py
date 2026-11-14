"""Nothing in this plugin can change the charger.

The owner's Easee login can start, stop and reconfigure the charger, and Easee
offers no read-only login. So the promise is kept by the code, and these tests
are what hold it, in three layers:

1. What ``src/`` may touch: only the adapter imports pyeasee, nothing imports
   an HTTP client, nothing calls anything named like a write, and nothing
   names a pyeasee method outside ``READ_ALLOWLIST``. The forbidden names are
   read from pyeasee itself, so a write method a new release adds is refused
   without anyone listing it.
2. What pyeasee does with those calls: each allowlisted method is followed
   through pyeasee's own source, helper by helper, and every body reached may
   send nothing but a GET, apart from the login. A ``get_`` name proves
   nothing: in 0.8.22 ``force_update_opmode`` posts a command.
3. What goes on the wire: every read is driven through the pretend cloud,
   which refuses anything but a GET and the login, and keeps what it saw.

What layer 2 cannot follow: a pyeasee object held in a local variable, a call
through ``super()``, a stored callback, or a method looked up by its name as a
string. Layer 1 refuses the last in ``src/``; for the others inside pyeasee,
layer 3 is the backstop, since whatever the path, the request reaches the wire.
"""

from __future__ import annotations

import ast
import asyncio
import collections.abc
import datetime as dt
import inspect
import pathlib
import textwrap
from typing import TYPE_CHECKING

import pyeasee.charger
import pyeasee.easee
import pyeasee.site
import pyeasee.throttler
import pytest

from easee_mcp.adapter import READ_ALLOWLIST, EaseeReader
from easee_mcp.fake import FAKE_CHARGER_ID, FAKE_PASSWORD, FAKE_USER, FakeEaseeCloud, WriteRefusedError

if TYPE_CHECKING:
    import types

SRC = pathlib.Path(__file__).parent.parent / "src"
MODULES = (pyeasee.easee, pyeasee.charger, pyeasee.site, pyeasee.throttler)
#: Any call of these names may put something other than a read on the wire.
WIRE_WRITES = frozenset({"post", "put", "patch", "delete", "send", "request", "ws_connect"})
#: The two pyeasee functions that may post: they log in, and change nothing on the charger.
LOGIN_PATHS = {("Easee", "connect"), ("Easee", "_refresh_token")}
LOGIN_URL = "/api/accounts/"
#: Clients that would reach Easee around the adapter.
HTTP_CLIENTS = (
    "aiohttp",
    "httpx",
    "requests",
    "urllib.request",
    "http.client",
    "websockets",
    "pysignalr",
    "socket",
)
#: Which pyeasee class an attribute of ``self`` holds.
HOLDERS = {"easee": "Easee", "site": "Site", "circuit": "Circuit"}


def _classes() -> dict[str, type]:
    return {
        name: value
        for module in MODULES
        for name, value in vars(module).items()
        if inspect.isclass(value) and value.__module__ == module.__name__
    }


CLASSES = _classes()


def _pyeasee_method_names() -> set[str]:
    """Every public method of every pyeasee class, except the plain mapping reads."""
    mapping = set(dir(collections.abc.Mapping))
    return {
        name
        for cls in CLASSES.values()
        for name, value in vars(cls).items()
        if callable(value) and not name.startswith("_") and name not in mapping
    }


def _sources() -> list[tuple[pathlib.Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in sorted(SRC.rglob("*.py"))]


def _imports(tree: ast.Module) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_only_the_adapter_imports_pyeasee():
    importers = [
        path.name for path, tree in _sources() if any(i.split(".")[0] == "pyeasee" for i in _imports(tree))
    ]
    assert importers == ["adapter.py"]


def test_no_http_client_is_imported_anywhere_in_src():
    found = [
        f"{path.name}: {name}"
        for path, tree in _sources()
        for name in _imports(tree)
        if any(name == client or name.startswith(client + ".") for client in HTTP_CLIENTS)
    ]
    assert found == []


def test_nothing_in_src_calls_anything_named_like_a_write():
    found = [
        f"{path.name}:{node.lineno} .{node.func.attr}"
        for path, tree in _sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WIRE_WRITES
    ]
    assert found == []


def test_src_names_no_pyeasee_method_outside_the_read_allowlist():
    forbidden = _pyeasee_method_names() - READ_ALLOWLIST
    # A name that is not a call still counts: a method passed on is a method called later.
    found = [
        f"{path.name}:{node.lineno} .{node.attr}"
        for path, tree in _sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]
    assert found == []


def test_no_pyeasee_method_is_reached_by_its_name_as_a_string():
    # getattr(charger, "start") names no attribute, so the test above would miss it.
    forbidden = _pyeasee_method_names() - READ_ALLOWLIST
    found = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "setattr", "delattr", "hasattr"}
                and len(node.args) >= 2  # the object, then the name
            ):
                continue
            name = node.args[1]
            literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
            if not literal or name.value in forbidden:
                found.append(f"{path.name}:{node.lineno} {node.func.id}")
    assert found == []


def test_the_forbidden_names_come_from_pyeasee_itself():
    forbidden = _pyeasee_method_names() - READ_ALLOWLIST
    for write in ("start", "stop", "pause", "resume", "toggle", "reboot", "set_dynamic_charger_current"):
        assert write in forbidden
    # Named like reads, and they post a command.
    assert {"force_update_opmode", "force_update_lifetimeenergy"} <= forbidden


# Layer 2: follow each allowlisted read through pyeasee's own source.


def _function(owner: str, name: str) -> types.FunctionType | None:
    if owner in CLASSES:
        value = inspect.getattr_static(CLASSES[owner], name, None)
    else:
        value = vars(next(m for m in MODULES if m.__name__ == owner)).get(name)
    # Only pyeasee's own code is followed; a mapping's get is the standard library's.
    return value if inspect.isfunction(value) and value.__module__.startswith("pyeasee") else None


def _tree(function: types.FunctionType) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(function)))


def _targets(owner: str, function: types.FunctionType) -> tuple[set[tuple[str, str]], list[ast.Call]]:
    """What a body calls inside pyeasee, and every call in it that may write to the wire."""
    module = function.__module__
    reached: set[tuple[str, str]] = set()
    writes: list[ast.Call] = []
    for node in ast.walk(_tree(function)):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Attribute):
            if called.attr in WIRE_WRITES:
                writes.append(node)
            held = called.value
            if isinstance(held, ast.Name) and held.id == "self" and owner in CLASSES:
                reached.add((owner, called.attr))
            elif (
                isinstance(held, ast.Attribute)
                and isinstance(held.value, ast.Name)
                and held.value.id == "self"
                and held.attr in HOLDERS
            ):
                reached.add((HOLDERS[held.attr], called.attr))
        elif isinstance(called, ast.Name):
            if called.id in CLASSES:
                reached.add((called.id, "__init__"))
            elif _function(module, called.id) is not None:
                reached.add((module, called.id))
    return reached, writes


def wire_writes_reached(start: set[tuple[str, str]], login: set[tuple[str, str]]) -> list[str]:
    """Every write to the wire any of ``start`` can reach, the login's own posts apart."""
    seen: set[tuple[str, str]] = set()
    queue = list(start)
    found: list[str] = []
    while queue:
        where = queue.pop()
        if where in seen:
            continue
        seen.add(where)
        function = _function(*where)
        if function is None:
            continue
        reached, writes = _targets(where[0], function)
        for call in writes:
            if where in login and _posts_to_login(call):
                continue
            found.append(f"{where[0]}.{where[1]}: .{call.func.attr}")  # type: ignore[attr-defined]
        queue.extend(reached - seen)
    return found


def _posts_to_login(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "post"
        and bool(call.args)
        and LOGIN_URL in ast.unparse(call.args[0])
    )


def _allowlisted() -> set[tuple[str, str]]:
    return {
        (owner, name) for owner in CLASSES for name in READ_ALLOWLIST if _function(owner, name) is not None
    }


def test_only_the_allowlisted_reads_reach_pyeasee():
    # Every allowlisted read reaches nothing but a GET or the login.
    assert wire_writes_reached(_allowlisted(), LOGIN_PATHS) == []


def test_every_allowlisted_name_is_found_in_pyeasee():
    # A name that resolves to nothing would be followed nowhere and pass for that reason.
    found = {name for _, name in _allowlisted()}
    assert found == READ_ALLOWLIST


def test_the_walk_finds_a_write_behind_a_helper():
    # pyeasee's own command methods are the proof the walk works: each reaches a post.
    for command in ("start", "force_update_opmode", "set_weekly_charge_plan"):
        assert wire_writes_reached({("Charger", command)}, LOGIN_PATHS), command
    # and the login's posts are allowed only because they go to the login.
    assert wire_writes_reached({("Easee", "connect")}, set())


# Layer 3: what goes on the wire.


async def _every_read(reader: EaseeReader) -> None:
    day = dt.datetime(2026, 11, 14, tzinfo=dt.UTC)
    try:
        await reader.chargers()
        await reader.state(FAKE_CHARGER_ID)
        await reader.limits(FAKE_CHARGER_ID)
        await reader.sessions(FAKE_CHARGER_ID, day, day + dt.timedelta(days=1))
        await reader.energy(FAKE_CHARGER_ID, day, day + dt.timedelta(days=1))
    finally:
        await reader.close()


def test_every_read_sends_only_gets_and_the_login():
    cloud = FakeEaseeCloud()
    asyncio.run(_every_read(EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=cloud.session())))
    assert cloud.refused == []
    sent = {(method, path) for method, path in cloud.requests if method != "GET"}
    assert sent == {("POST", "/api/accounts/login")}


def test_the_pretend_cloud_refuses_a_write():
    cloud = FakeEaseeCloud()
    command = f"https://api.easee.com/api/chargers/{FAKE_CHARGER_ID}/commands/start_charging"
    with pytest.raises(WriteRefusedError):
        asyncio.run(cloud.session().post(command))
    assert cloud.refused == [("POST", f"/api/chargers/{FAKE_CHARGER_ID}/commands/start_charging")]


def test_every_tool_sends_only_gets_and_the_login():
    from easee_mcp.answers import ChargerAnswers  # noqa: PLC0415 - the server is a slow import
    from easee_mcp.cache import CachedCharger  # noqa: PLC0415
    from easee_mcp.server import TOOLS, build_server  # noqa: PLC0415

    cloud = FakeEaseeCloud()
    reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=cloud.session())
    server = build_server(ChargerAnswers(CachedCharger(reader, now=lambda: dt.datetime.now(dt.UTC))))

    async def every_tool():
        for name in TOOLS:
            answer = (await server.call_tool(name, {})).structured_content
            assert answer["success"] is True, name

    asyncio.run(every_tool())
    assert cloud.refused == []
    assert {(m, p) for m, p in cloud.requests if m != "GET"} == {("POST", "/api/accounts/login")}
