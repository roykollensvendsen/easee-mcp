# easee-mcp

An Easee car charger's status, read by voice: a read-only MCP server and claude-voice plugin.

It answers four questions by voice: is the car charging and how fast, what
current the charger may give, how many charging sessions there were lately,
and how much energy went in today, this week or this month. It only reads.
It never starts, stops or changes the charger, and a test proves it cannot;
see [ADR-EM-001](decisions/ADR-EM-001-read-only-kept-by-the-code.md).

Try it on a made-up charger, with no login and nothing sent:

```console
$ easee-mcp status --simulate
Simulated: Garasje is charging at 7.2 kW, 12.4 kWh this session. It is limited to 16 A.
```

For the real charger, save the Easee username and password with `easee-mcp
login`. It asks for both without showing what you type, and saves them as `user`
and `password` in `~/.config/easee-mcp/`, readable by you alone. It sends nothing
to Easee; `easee-mcp status` then shows whether the login works. The login is
read only when a question is asked, and never printed or logged. The charger is
found on the account each time the plugin starts; its ID is never written
anywhere.

```console
$ easee-mcp login --help
usage: easee-mcp login [-h] [--login-dir LOGIN_DIR]

options:
  -h, --help            show this help message and exit
  --login-dir LOGIN_DIR
                        the folder holding 'user' and 'password' (default ~/.config/easee-mcp)
```

`claude-voice plugins install` adds the plugin from `claude-voice-plugin.toml`,
and names `uv run easee-mcp login` as the step only you can do.

A voice session starts it as an MCP server over stdio:

```console
$ easee-mcp serve --help
usage: easee-mcp serve [-h] [--simulate] [--login-dir LOGIN_DIR]

options:
  -h, --help            show this help message and exit
  --simulate            answer from a made-up charger
  --login-dir LOGIN_DIR
                        the folder holding 'user' and 'password' (default ~/.config/easee-mcp)
```

Easee allows the charger list twice a minute, and sessions and energy ten
reads an hour each. So the plugin keeps the state for 30 seconds, sessions for
ten minutes and energy for thirty. Past a limit it sends nothing, and gives the
last answer with its age, or says how long to wait.

## The gates

Every one of these runs in continuous integration, and all of them run here:

```console
$ uv run ruff check .
All checks passed!
```

<!-- not run: the workflow runs each of these; nesting them here would run the suite inside itself -->
```bash
uv run ruff format --check .   # layout
uv run mypy src                # types
uv run pytest -q               # behaviour
python scripts/mutate.py       # that each rule's own test can fail
uv run python scripts/ratchet.py check --base origin/main  # that nothing got worse
```

The last one measures coverage, the rules' own tests, the test count, lint and
type errors, import cycles, imports that cross the layers, copied code and the
most tangled function per module. A pull request may make none of them worse
unless its text has a line `Ratchet-Exception: <measure> - the reason`. Main
keeps what it measured with its own CI run, for the code-health page;
`quality.json` holds only the suite's time budget, and no measured number is
ever committed.

`CONTRIBUTING.md` says what each one is for, and
[`decisions/`](decisions/README.md) records the choices that shaped them,
including how a change is made here.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
