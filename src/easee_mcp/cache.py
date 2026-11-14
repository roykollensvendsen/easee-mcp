"""Keep Easee's answers, so a chatty voice stays inside Easee's limits.

Easee publishes a limit per endpoint (developer.easee.com/reference): the
charger list twice a minute, sessions and energy ten times an hour each. A
voice can ask "is it charging?" every few seconds, so:

* the charger list is read once, and kept for the life of the process;
* the state and limits are kept thirty seconds;
* sessions are kept ten minutes, and energy thirty;
* past a limit, or while Easee's Retry-After runs, nothing is sent: the last
  answer is given with its age, or the wait is said.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from easee_mcp.adapter import SlowDownError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from easee_mcp.adapter import ChargerRef, EaseeReader, Limits, Session

#: How long each answer is kept, in seconds.
STATE_SECONDS = 30
SESSIONS_SECONDS = 600
ENERGY_SECONDS = 1800
#: (calls, per seconds) Easee allows, per endpoint.
LIMITS: dict[str, tuple[int, int]] = {
    "chargers": (2, 60),
    "sessions": (10, 3600),
    "energy": (10, 3600),
}
#: The periods energy can be asked for.
PERIODS = ("day", "week", "month")


@dataclass(frozen=True)
class Aged[T]:
    """An answer, and how many seconds old it is."""

    value: T
    age_seconds: int


@dataclass(frozen=True)
class Snapshot:
    """The state and the limits, and how old they are."""

    state: object
    limits: Limits
    age_seconds: int


class CachedCharger:
    """The owner's charger, read through the cache."""

    def __init__(self, reader: EaseeReader, *, now: Callable[[], dt.datetime]) -> None:
        """Read through ``reader``, with ``now`` as the clock."""
        self._reader = reader
        self._now = now
        self._charger: ChargerRef | None = None
        self._kept: dict[str, tuple[dt.datetime, object]] = {}
        self._calls: dict[str, list[dt.datetime]] = {}
        self._until: dict[str, dt.datetime] = {}

    async def which(self) -> ChargerRef:
        """The charger on the account, read once."""
        if self._charger is None:
            found = await self._ask("chargers", self._reader.chargers)
            if not found:
                raise LookupError
            self._charger = found[0]
        return self._charger

    async def reading(self) -> Snapshot:
        """The state and the limits, at most thirty seconds old unless Easee says wait."""
        charger = await self.which()
        # RULE: the state is read at most every thirty seconds
        got = await self._kept_for(
            "reading", STATE_SECONDS, "state", lambda: self._reader.reading(charger.id)
        )
        return Snapshot(state=got.value.state, limits=got.value.limits, age_seconds=got.age_seconds)

    async def sessions(self, *, days: int) -> Aged[list[Session]]:
        """The sessions of the last ``days`` days, newest first."""
        charger = await self.which()
        end = self._now()
        start = end - dt.timedelta(days=days)
        return await self._kept_for(
            f"sessions:{days}",
            SESSIONS_SECONDS,
            "sessions",
            lambda: self._reader.sessions(charger.id, start, end),
        )

    async def energy(self, period: str) -> Aged[float]:
        """The kWh charged since the start of this day, week or month."""
        charger = await self.which()
        end = self._now()
        start = period_start(end, period)
        return await self._kept_for(
            f"energy:{period}",
            ENERGY_SECONDS,
            "energy",
            lambda: self._reader.energy(charger.id, start, end),
        )

    def calls_left_hour(self) -> int:
        """Calls left this hour on the tighter of the two hourly limits."""
        return min(self._left(key) for key in ("sessions", "energy"))

    async def close(self) -> None:
        """Close the connection to Easee."""
        await self._reader.close()

    async def _kept_for[T](
        self, key: str, seconds: int, limit: str, read: Callable[[], Awaitable[T]]
    ) -> Aged[T]:
        now = self._now()
        kept = self._kept.get(key)
        if kept is not None and (now - kept[0]).total_seconds() < seconds:
            return Aged(kept[1], int((now - kept[0]).total_seconds()))  # type: ignore[arg-type]
        try:
            value = await self._ask(limit, read)
        except SlowDownError:
            if kept is None:
                raise
            return Aged(kept[1], int((now - kept[0]).total_seconds()))  # type: ignore[arg-type]
        self._kept[key] = (now, value)
        return Aged(value, 0)

    async def _ask[T](self, limit: str, read: Callable[[], Awaitable[T]]) -> T:
        now = self._now()
        until = self._until.get(limit)
        # RULE: a 429 stops calls until retry after passes
        if until is not None and now < until:
            raise SlowDownError(max(1, int((until - now).total_seconds() + 0.999)))
        if limit in LIMITS:
            allowed, period = LIMITS[limit]
            recent = self._recent(limit)
            # RULE: sessions are read at most ten times an hour
            if len(recent) >= allowed:
                raise SlowDownError(int((recent[0] + dt.timedelta(seconds=period) - now).total_seconds()) + 1)
            self._calls[limit] = [*recent, now]
        try:
            return await read()
        except SlowDownError as slow:
            self._until[limit] = now + dt.timedelta(seconds=slow.seconds)
            raise

    def _recent(self, limit: str) -> list[dt.datetime]:
        _, period = LIMITS[limit]
        since = self._now() - dt.timedelta(seconds=period)
        return [t for t in self._calls.get(limit, []) if t > since]

    def _left(self, limit: str) -> int:
        return max(0, LIMITS[limit][0] - len(self._recent(limit)))


def period_start(now: dt.datetime, period: str) -> dt.datetime:
    """The start of this day, week (from Monday) or month, in local time."""
    local = now.astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight
    if period == "week":
        return midnight - dt.timedelta(days=midnight.weekday())
    if period == "month":
        return midnight.replace(day=1)
    raise ValueError(period)
