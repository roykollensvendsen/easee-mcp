"""The status file the voice bridge reads, and the one file this plugin writes.

Its shape is shared by every voice plugin: ``{"at", "ok", "say", "figures"}``,
at ``~/.local/state/easee-mcp/status.json``. It says whether the last read
worked, what the charger was last seen doing, and how many reads are left this
hour. It never holds the charger's ID, where it stands, or when a session
began or ended, since those tell when someone is home.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable

#: Where the bridge looks for it when the plugin entry names no other place.
STATUS_FILE = pathlib.Path("~/.local/state/easee-mcp/status.json")
#: The bridge shows this much of ``say``, and so many figures.
SAY_MAX = 120
FIGURES_MAX = 8


class StatusFile:
    """The status file: replaced whole, and only when what it says changes."""

    def __init__(self, path: pathlib.Path, *, now: Callable[[], dt.datetime]) -> None:
        """Write to ``path``, stamping each version with ``now``."""
        self._path = path
        self._now = now
        self._last: tuple[bool, str, tuple[tuple[str, float], ...]] | None = None
        self._seen = ""
        self._figures: dict[str, float] = {}

    def after(self, answer: dict[str, Any], calls_left_hour: int) -> None:
        """Record what a tool just answered."""
        if "seen" in answer:
            self._seen = str(answer["seen"])
            self._figures = {
                name: float(answer[key])
                for name, key in (("kw_now", "power_kw"), ("session_kwh", "session_kwh"))
                if answer.get(key) is not None
            }
        figures = {**self._figures, "calls_left_hour": calls_left_hour}
        if answer.get("success"):
            left = f"{calls_left_hour} of 10 session reads left this hour"
            say = f"{self._seen}; {left}" if self._seen else left
            self.record(ok=True, say=say, figures=figures)
        else:
            self.record(ok=False, say=str(answer.get("say", "")), figures=figures)

    def record(self, *, ok: bool, say: str, figures: dict[str, float]) -> None:
        """Write the file, unless it would say what it already says."""
        kept = dict(list(figures.items())[:FIGURES_MAX])
        current = (ok, say[:SAY_MAX], tuple(sorted(kept.items())))
        if current == self._last:
            return
        body = {"at": self._now().isoformat(), "ok": ok, "say": say[:SAY_MAX], "figures": kept}
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self._path.parent, prefix=".status-", suffix=".json")
        try:
            try:
                out = os.fdopen(handle, "w")
            except BaseException:
                os.close(handle)
                raise
            with out:
                json.dump(body, out, indent=1)
            # RULE: the status file is replaced whole never written in place
            pathlib.Path(name).replace(self._path)
        except BaseException:
            # RULE: a failed status write leaves no temporary
            pathlib.Path(name).unlink(missing_ok=True)
            raise
        self._last = current
