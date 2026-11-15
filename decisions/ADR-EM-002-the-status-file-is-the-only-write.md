# ADR-EM-002: The status file is the only file the plugin writes

## Status

Accepted on the voice architect's advice, 2026-11-15. Amended by
[ADR-EM-003](ADR-EM-003-the-owner-saves-the-login-by-hand.md): the owner's own
`easee-mcp login` saves the login; the server still writes only the status file.

## Context

The voice bridge shows each plugin's own status from a small JSON file the
plugin writes (the shared plugin-status design of 2026-11-14). This plugin
also holds things that must never land on disk: the owner's login, the
charger's ID, the site's place, and session times, which tell when someone is
home. Easee's refresh token works once, and keeping it would mean a second file.

## Options considered

- **No file at all.** Health would then show nothing for this plugin.
- **A cache on disk, as kassalapp-mcp keeps.** Not needed: the limits that
  matter are per hour, and a restart costing one login and one read is cheap.
- **Persist the refresh token now.** Deferred to the phase that adds commands,
  as its own reviewed change; a restart logs in again, the documented path.
- **The status file only.** Chosen.

## Decision

The plugin writes exactly one file, `~/.local/state/easee-mcp/status.json`, in
the shared shape `{at, ok, say, figures}`, replaced whole and only when what it
says changes. It holds the charger's last-seen mode and power, the session
energy and the reads left this hour. `tests/test_status_file.py` refuses a
file write anywhere in `src/` outside `status.py`, and a status holding the
ID, the place or a session time.

## Consequences

- The login and every Easee answer live in memory only; a restart forgets all.
- A second file later (the refresh token) supersedes this record.
- Worse: the file says what the charger was doing when last asked, which is
  itself a hint of activity at home, readable by anything that can read the
  owner's state folder.

## Related

- `src/easee_mcp/status.py`, `tests/test_status_file.py`
