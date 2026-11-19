"""Keep Easee's answers, so a chatty voice stays inside Easee's limits.

Easee publishes a limit per endpoint (developer.easee.com/reference): the
charger list twice a minute, sessions and energy ten times an hour each. A
voice can ask "is it charging?" every few seconds, so:

* the charger list is read once, and kept for the life of the process;
* the state and limits are kept thirty seconds;
* sessions are kept ten minutes, and energy thirty;
* past a limit, or while Easee's Retry-After runs, nothing is sent: the last
  answer is given with its age, or the wait is said;
* a read Easee refuses lists the chargers once more, so a charger that has left
  the account is said as gone, and nothing kept from it is given again.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from easee_mcp.adapter import ChargerGoneError, NotAllowedError, NotFoundError, SlowDownError

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
            self._charger = _first(await self._ask("chargers", self._reader.chargers))
            if self._charger is None:
                raise LookupError
        return self._charger

    async def reading(self) -> Snapshot:
        """The state and the limits, at most thirty seconds old unless Easee says wait."""
        charger = await self.which()
        # RULE: the state is read at most every thirty seconds
        got = await self._kept_for(
            "reading", STATE_SECONDS, "state", lambda: self._of(charger, self._reader.reading)
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
            lambda: self._of(charger, lambda i: self._reader.sessions(i, start, end)),
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
            lambda: self._of(charger, lambda i: self._reader.energy(i, start, end)),
        )

    def calls_left_hour(self) -> int:
        """Calls left this hour on the tighter of the two hourly limits."""
        return min(self._left(key) for key in ("sessions", "energy"))

    async def close(self) -> None:
        """Close the connection to Easee."""
        await self._reader.close()

    async def _of[T](self, charger: ChargerRef, read: Callable[[str], Awaitable[T]]) -> T:
        """Read ``charger``; when Easee refuses, list the chargers once to learn whether it left."""
        try:
            return await read(charger.id)
        except (NotAllowedError, NotFoundError):
            # RULE: a refusal is never said as a wait of our own making
            if self._may("chargers"):  # else only Easee's own 429 could say wait
                await self._unless_gone(charger)
            raise

    async def _unless_gone(self, charger: ChargerRef) -> None:
        """List the chargers once, and raise ChargerGoneError if ``charger`` is not among them."""
        try:
            found = await self._ask("chargers", self._reader.chargers)
        except SlowDownError:
            raise  # Easee's 429: said as the wait, and never retried here
        except Exception:  # noqa: BLE001 - the list failing tells nothing; the refusal stands
            return
        # RULE: a charger that left the account is said as gone
        if all(c.id != charger.id for c in found):
            # The list just read picks the next charger by the rule which() uses,
            # so the next question needs no third list within Easee's minute.
            self._charger = _first(found)
            self._kept.clear()
            raise ChargerGoneError(tuple(c.name for c in found)) from None

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

    def _may(self, limit: str) -> bool:
        """Whether a call to ``limit`` would be sent now, rather than told to wait."""
        until = self._until.get(limit)
        return (until is None or self._now() >= until) and self._left(limit) > 0

    def _recent(self, limit: str) -> list[dt.datetime]:
        _, period = LIMITS[limit]
        since = self._now() - dt.timedelta(seconds=period)
        return [t for t in self._calls.get(limit, []) if t > since]

    def _left(self, limit: str) -> int:
        return max(0, LIMITS[limit][0] - len(self._recent(limit)))


def _first(found: list[ChargerRef]) -> ChargerRef | None:
    """The charger to read from a list of the account's: the first, as no choice is made yet."""
    return found[0] if found else None


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
