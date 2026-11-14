# ADR-EM-001: Read-only is kept by the code, and proved by a test

## Status

Accepted by the owner, through the manager, 2026-11-14; the design checked by
the voice architect on 2026-11-15.

## Context

The plugin logs in with the owner's own Easee account. Easee documents no
scoped or read-only token (developer.easee.com/docs/authentication-1), so the
same login that reads the charger can start, stop, reboot and reconfigure it.
The first version is to read only: status, current, sessions and energy.

In pyeasee 0.8.22 a `get_` prefix does not mean a read:
`force_update_opmode` and `force_update_lifetimeenergy` post commands, and
`set_weekly_charge_plan` reads and then posts. The library also exposes
generic `post`, `put` and `delete` helpers.

## Options considered

- **Trust the method names.** Refused: the names above are reads in name only.
- **A second Easee user with fewer rights.** Worth having, and up to the owner
  in the Easee app; it still does not stop this code from trying a write.
- **Call Easee's HTTP API directly, without pyeasee.** Fewer moving parts to
  audit, but the token refresh, its single-use rule and the throttling would
  be written again, untested against the live service.
- **pyeasee behind one adapter with an allowlist, and tests that follow the
  calls.** Chosen.

## Decision

Only `src/easee_mcp/adapter.py` imports pyeasee, and it calls only the names in
`READ_ALLOWLIST`. `tests/test_read_only.py` refuses, in `src/`: any other
pyeasee method name (read from pyeasee itself), any call named like a write,
and any HTTP client. It follows every allowlisted method through pyeasee's own
source and fails if any path reaches anything but a GET or the login. And it
drives every read through a pretend cloud that refuses anything else.

## Consequences

- A pyeasee upgrade is a deliberate change: the version is pinned exactly,
  and the test reads that version's source.
- Adding a command later means changing the allowlist and this record in the
  same reviewed change, with a spoken yes for each command.
- The state and the limits are read as numbered observations
  (`get_observations`), the way pyeasee's own tool reads them; the older
  state read refused the owner's login on 2026-11-15.
- Worse: an observation ID that Easee renumbers reads as unknown rather than
  failing, so a wrong number is said as "not known", not caught by a test.

## Related

- `src/easee_mcp/adapter.py`, `tests/test_read_only.py`, `src/easee_mcp/fake.py`
