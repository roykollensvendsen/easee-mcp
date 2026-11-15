"""The install manifest is valid, and says what this plugin really serves.

`claude-voice-plugins install` reads `claude-voice-plugin.toml` to add the plugin.
It is checked here against the bridge's own format, copied into
`references/claude-voice/`, and against the server: the voice may reach only the
tools this read-only server has, and the manifest holds nothing of the owner's.
"""

import asyncio
import json
import pathlib
import tomllib

import jsonschema

from easee_mcp.cli import COMMANDS, _answers
from easee_mcp.server import TOOLS, build_server

ROOT = pathlib.Path(__file__).parent.parent
MANIFEST = ROOT / "claude-voice-plugin.toml"
FORMAT = ROOT / "references/claude-voice/plugin-manifest-v1.json"


def _manifest():
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_is_valid_in_the_bridges_format():
    schema = json.loads(FORMAT.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(_manifest())


def test_the_manifest_lists_exactly_the_tools_the_server_serves():
    server = build_server(_answers(simulate=True, login_dir=None), simulated=True)
    served = sorted(tool.name for tool in asyncio.run(server.list_tools()))
    assert sorted(_manifest()["tools"]) == served == sorted(TOOLS)


def test_the_manifest_starts_the_server_this_package_has():
    manifest = _manifest()
    assert (manifest["name"], manifest["command"], manifest["args"]) == (
        "easee",
        "uv",
        ["run", "easee-mcp", "serve"],
    )


def test_the_login_is_asked_by_this_plugins_own_masked_command():
    """The bridge names this step and never runs it; the password is never a manifest field."""
    step = _manifest()["secrets_step"].split()
    assert step[:3] == ["uv", "run", "easee-mcp"]
    assert step[3] in COMMANDS


def test_the_manifest_holds_nothing_of_the_owners():
    """No config to fill in: the charger is found on the account each time, and the login has its own step."""
    manifest = _manifest()
    assert "config" not in manifest
    assert "uses" not in manifest
