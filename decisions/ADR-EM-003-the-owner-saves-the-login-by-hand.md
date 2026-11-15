# ADR-EM-003: The owner saves the login with the plugin's own command

## Status

Accepted, 2026-11-15. Amends [ADR-EM-002](ADR-EM-002-the-status-file-is-the-only-write.md).

## Context

The bridge's install command (`claude-voice plugins install`, the manifest
format `docs/plugin-manifest/v1.json`) never asks for a secret itself. It names
the plugin's own command for it, as `secrets_step`, and leaves that step to the
owner. Until now the owner made the two login files by hand. The manager asked
that the login be asked only through a prompt that shows nothing typed.

## Options considered

- **Keep making the files by hand.** Install would have no step to name, and
  getting the rights on the files right is left to the owner each time.
- **Let install ask.** The format forbids it: a secret is never a personal field.
- **A `login` command in the plugin.** Chosen.

## Decision

`easee-mcp login` asks for the username and password with a prompt that shows
nothing typed. It saves them as `user` and `password` in `~/.config/easee-mcp/`:
each file is replaced whole and made `0600`, and the folder is made `0700`. It
sends nothing to Easee and prints neither. An empty answer saves nothing and
keeps what was there.

The server still writes only the status file. `tests/test_status_file.py`
allows a write outside `status.py` only inside `save_login()`, and proves that
only the `login` verb calls it.

## Consequences

- A second writer exists, but only the owner, at the terminal, ever runs it.
- Whether the login works shows only at the next `easee-mcp status`. A check
  against Easee here would mean a login request from a command meant only to save.

## Related

- `src/easee_mcp/cli.py`, `tests/test_login.py`, `claude-voice-plugin.toml`
