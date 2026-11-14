"""What the voice says about the charger: one or two sentences, and the figures.

Every answer is a dict with ``success``, ``outcome`` and ``say``, the sentence
read aloud, beside the figures it was made from. A figure Easee did not send is
left out of the sentence and given as None; it is never said as zero. The
charger's ID never appears; its name, which comes from the cloud, is said only
as one short line.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from easee_mcp.cache import PERIODS

if TYPE_CHECKING:
    import datetime as dt

    from easee_mcp.adapter import ChargerState
    from easee_mcp.cache import CachedCharger

#: The longest charger name said, and what is said when it is empty.
NAME_MAX = 40
NO_NAME = "The charger"
#: What each operating mode is said as, after the charger's name; from
#: pyeasee's STATUS table and Easee's charger-mode reference.
MODES: dict[int, str] = {
    0: "is offline",
    1: "has no car connected",
    2: "has a car connected, waiting to start",
    3: "is charging",
    4: "has finished charging; the car is still connected",
    5: "is in error",
    6: "is ready to charge",
    7: "is waiting for the car to be authorised",
    8: "is de-authorising",
}
CHARGING = 3
#: An answer older than this says its age.
SAY_AGE_FROM = 30
MAX_DAYS = 31
PERIOD_WORDS = {"day": "today", "week": "this week", "month": "this month"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def one_line(name: str) -> str:
    """A name from the cloud as the voice may say it: one line, short, never empty."""
    # RULE: a charger name is said as one short line
    flat = " ".join(_CONTROL.sub(" ", name).split())[:NAME_MAX].strip()
    return flat or NO_NAME


class ChargerAnswers:
    """The four questions about the charger, answered in sentences."""

    def __init__(self, charger: CachedCharger, *, tz: dt.tzinfo | None = None) -> None:
        """Answer from ``charger``, saying times in ``tz`` (local time by default)."""
        self._charger = charger
        self._tz = tz

    async def status(self) -> dict[str, Any]:
        """Is it charging, how fast, how much this session, and the limit."""
        name = one_line((await self._charger.which()).name)
        got = await self._charger.reading()
        state: ChargerState = got.state  # type: ignore[assignment]
        limit, top = got.limits.dynamic_charger_a, got.limits.max_charger_a
        charging = state.op_mode == CHARGING
        say = f"{name} {_mode(state.op_mode)}"
        if charging and state.power_kw is not None:
            say += f" at {state.power_kw:.1f} kW"
        if state.session_kwh is not None:
            say += f", {state.session_kwh:.1f} kWh this session"
        say += "."
        if limit is not None and (top is None or limit < top):
            say += f" It is limited to {limit:.0f} A."
        return {
            "success": True,
            "outcome": "ok",
            "say": say + _age(got.age_seconds),
            "charging": charging,
            "op_mode": state.op_mode,
            "power_kw": state.power_kw,
            "session_kwh": state.session_kwh,
            "limit_a": limit,
            "age_seconds": got.age_seconds,
        }

    async def current(self) -> dict[str, Any]:
        """The currents the charger may give now, and its own and the circuit's maximum."""
        name = one_line((await self._charger.which()).name)
        got = await self._charger.reading()
        limits = got.limits
        state: ChargerState = got.state  # type: ignore[assignment]
        parts = []
        if limits.dynamic_charger_a is not None:
            first = f"{name} may give the car up to {limits.dynamic_charger_a:.0f} A now"
            if state.output_current_a:
                first += f", and offers it {state.output_current_a:.0f} A"
            parts.append(first + ".")
        tops = []
        if limits.max_charger_a is not None:
            tops.append(f"The charger's own maximum is {limits.max_charger_a:.0f} A")
        circuit = [a for a in limits.circuit_max_a if a is not None]
        if circuit:
            tops.append(f"the circuit's is {min(circuit):.0f} A")
        if tops:
            parts.append(", and ".join(tops) + ".")
        say = " ".join(parts) or f"{name} did not report its limits."
        return {
            "success": True,
            "outcome": "ok",
            "say": say + _age(got.age_seconds),
            "dynamic_charger_a": limits.dynamic_charger_a,
            "max_charger_a": limits.max_charger_a,
            "circuit_max_a": list(limits.circuit_max_a),
            "dynamic_circuit_a": list(limits.dynamic_circuit_a),
            "output_current_a": state.output_current_a,
            "age_seconds": got.age_seconds,
        }

    async def sessions(self, days: int = 7) -> dict[str, Any]:
        """How many sessions in the last days, their energy, and the latest."""
        days = max(1, min(MAX_DAYS, int(days)))
        got = await self._charger.sessions(days=days)
        found = got.value
        if not found:
            say = f"No charging sessions in the last {days} days."
        else:
            total = sum(s.kwh for s in found)
            latest = found[0]
            began = latest.connected.astimezone(self._tz)
            count = f"{len(found)} charging session{'s' if len(found) != 1 else ''}"
            say = (
                f"{count} in the last {days} days, {total:.1f} kWh in all. "
                f"The latest began {began:%A} at {began:%H:%M} and gave {latest.kwh:.1f} kWh."
            )
        return {
            "success": True,
            "outcome": "ok",
            "say": say + _age(got.age_seconds),
            "sessions": len(found),
            "kwh": round(sum(s.kwh for s in found), 2),
            "days": days,
            "age_seconds": got.age_seconds,
        }

    async def energy(self, period: str = "day") -> dict[str, Any]:
        """The kWh charged today, this week or this month."""
        # RULE: an unknown period is asked about never guessed
        if period not in PERIODS:
            return {
                "success": False,
                "outcome": "which_period",
                "say": "Which period: today, this week or this month?",
            }
        got = await self._charger.energy(period)
        return {
            "success": True,
            "outcome": "ok",
            "say": f"{got.value:.1f} kWh charged {PERIOD_WORDS[period]}." + _age(got.age_seconds),
            "kwh": got.value,
            "period": period,
            "age_seconds": got.age_seconds,
        }


def _mode(op_mode: int | None) -> str:
    if op_mode is None:
        return "did not say what it is doing"
    return MODES.get(op_mode, f"reports a state I don't know ({op_mode})")


def _age(seconds: int) -> str:
    if seconds < SAY_AGE_FROM:
        return ""
    minutes = round(seconds / 60)
    if minutes < 1:
        return f" As of {seconds} seconds ago."
    return f" As of {minutes} minute{'s' if minutes != 1 else ''} ago."
