# Setting up easee-mcp

This is the setup guide, written so an agent can follow it alone, top to bottom. Each step is a
command, then a `Check:` line naming the command whose output proves the step worked. The
steps that need the owner are gathered at the end.

It needs [uv](https://docs.astral.sh/uv/) and a checkout of this repository. The plugin only
reads from the charger, so nothing here can start, stop or change it.

## Steps

### 1. Install the plugin and its tools in the checkout

<!-- not run: it installs; the suite runs inside what it installs -->
```bash
uv sync --all-groups --all-extras
```

Check: `uv run easee-mcp --help` prints the plugin's usage.

### 2. Try it on a made-up charger, with no login and nothing sent

```console
$ easee-mcp status --simulate
Simulated: Garasje is charging at 7.2 kW, 12.4 kWh this session. It is limited to 16 A.
```

Check: `easee-mcp status --simulate` starts with "Simulated:".

### 3. See where the login is read from

Nothing is saved by this step.

```console
$ easee-mcp login --help
usage: easee-mcp login [-h] [--login-dir LOGIN_DIR]

options:
  -h, --help            show this help message and exit
  --login-dir LOGIN_DIR
                        the folder holding 'user' and 'password' (default ~/.config/easee-mcp)
```

Check: `easee-mcp login --help` names the folder `~/.config/easee-mcp`.

### 4. See how a voice session starts it

A voice session starts it as an MCP server over stdio.

```console
$ easee-mcp serve --help
usage: easee-mcp serve [-h] [--simulate] [--login-dir LOGIN_DIR]

options:
  -h, --help            show this help message and exit
  --simulate            answer from a made-up charger
  --login-dir LOGIN_DIR
                        the folder holding 'user' and 'password' (default ~/.config/easee-mcp)
```

Check: `easee-mcp serve --help` lists `--simulate`.

### 5. Show what installing it in claude-voice would change

The installer reads `claude-voice-plugin.toml` and changes nothing with `--dry-run`. See
claude-voice's own README, "Installing a plugin in one command".

<!-- not run: it needs claude-voice and this checkout's path -->
```bash
claude-voice plugins install easee --from <this checkout> --dry-run
```

Check: the dry run lists each file it would change, and asks nothing.

## Steps only the owner can do

<!-- owner -->
### 6. Save the Easee login

Save the Easee username and password as `user` and `password` in `~/.config/easee-mcp/`,
readable by the owner alone. Use `uv run easee-mcp login`, which asks without showing what is
typed, or an agent's masked input box (secret-box) that writes those two files. Never put a
value in a chat, a command line or a file in this repository.

<!-- not run: it asks for the login -->
```bash
uv run easee-mcp login
```

Check: `uv run easee-mcp status` says the charger's state, not a missing or refused login.

Only the owner can do it, because the login is the owner's, and it could also change the
charger. The plugin finds the charger on the account each time it starts, and never writes its
ID anywhere.

<!-- owner -->
### 7. Install and approve the plugin in claude-voice

It asks questions, so it runs only in a terminal of the owner's own, and ends with the owner's
typed approval.

<!-- not run: it asks for typed input -->
```bash
claude-voice plugins install easee --from <this checkout>
```

Check: `claude-voice plugins list` shows `easee` approved.

Only the owner can do it, because approving is what lets the voice offer the tools.
