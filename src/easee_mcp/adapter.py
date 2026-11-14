"""The one door to Easee: reads through pyeasee, and lets only chosen fields out.

This module is the only one that imports pyeasee, and it calls only the methods
in ``READ_ALLOWLIST``. The owner's login can also change the charger, so the
read-only promise is kept here, by what this code calls, and proved by
``tests/test_read_only.py``. What comes back is copied field by field into small
records; anything else Easee sends, such as the site's address, stays here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyeasee  # type: ignore[import-untyped]
from pyeasee import exceptions

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Awaitable, Callable

# pyeasee logs the username and the token at debug, and URLs that hold the
# charger's ID at warning. None of it may reach a log.
for _library in ("pyeasee", "pysignalr"):
    # RULE: the library never logs
    logging.getLogger(_library).setLevel(logging.CRITICAL + 1)

#: The pyeasee methods this plugin may call, and nothing else. Each is a read:
#: ``tests/test_read_only.py`` follows every one through pyeasee's own source
#: and fails if any path reaches anything but a GET, or the login.
READ_ALLOWLIST: frozenset[str] = frozenset(
    {
        "connect",  # logs in; changes nothing on the charger
        "close",
        "get_chargers",
        "get_observations",  # the state and the limits, by observation ID
        "get_sessions_between_dates",
        "get_consumption_between_dates",
    }
)
#: Easee's observation IDs, from pyeasee's ChargerStreamData table; each is
#: readable by an ordinary user. Nothing else is asked for, so the charger's
#: location (observation 25) is never fetched.
OBSERVATIONS: dict[str, int] = {
    "circuitMaxCurrentP1": 22,
    "circuitMaxCurrentP2": 23,
    "circuitMaxCurrentP3": 24,
    "maxChargerCurrent": 47,
    "dynamicChargerCurrent": 48,
    "reasonForNoCurrent": 96,
    "chargerOpMode": 109,
    "dynamicCircuitCurrentP1": 111,
    "dynamicCircuitCurrentP2": 112,
    "dynamicCircuitCurrentP3": 113,
    "outputCurrent": 114,
    "totalPower": 120,
    "sessionEnergy": 121,
}
#: Seconds one read may take. The voice gives up on a tool after about 15.
TIMEOUT = 8.0
#: The wait to say when Easee answers 429 without saying how long.
DEFAULT_WAIT = 60


class CloudDownError(Exception):
    """Easee's cloud did not answer, or answered with something unreadable."""


class LoginRefusedError(Exception):
    """Easee refused the username or password."""


class NotAllowedError(Exception):
    """Easee logged in, and refused this login that read."""


class SlowDownError(Exception):
    """Easee said to slow down."""

    def __init__(self, seconds: int) -> None:
        """Remember how long Easee said to wait."""
        super().__init__(seconds)
        self.seconds = seconds


@dataclass(frozen=True)
class ChargerRef:
    """A charger on the account: its ID, never shown unmasked, and its name."""

    id: str
    name: str


@dataclass(frozen=True)
class ChargerState:
    """What the charger is doing now."""

    op_mode: int | None
    power_kw: float | None
    session_kwh: float | None
    output_current_a: float | None
    dynamic_current_a: float | None
    reason_for_no_current: int | None


@dataclass(frozen=True)
class Limits:
    """The currents the charger may give, as read."""

    max_charger_a: float | None
    dynamic_charger_a: float | None
    circuit_max_a: tuple[float | None, float | None, float | None]
    dynamic_circuit_a: tuple[float | None, float | None, float | None]


@dataclass(frozen=True)
class Session:
    """One charging session: when the car came and went, and the energy."""

    connected: dt.datetime
    disconnected: dt.datetime | None
    kwh: float


def mask(charger_id: str) -> str:
    """The charger's ID as a log may show it: its first two and last two characters."""
    return f"{charger_id[:2]}…{charger_id[-2:]}" if len(charger_id) > 4 else "…"  # noqa: PLR2004


class EaseeReader:
    """Reads one Easee account, and nothing but reads."""

    def __init__(self, login: Callable[[], tuple[str, str]], *, session: Any = None) -> None:  # noqa: ANN401
        """Read with the login ``login`` gives, only when the first read is asked for."""
        self._login = login
        self._session = session
        self._easee: Any = None
        self._chargers: dict[str, Any] = {}

    async def chargers(self) -> list[ChargerRef]:
        """The chargers on the account."""
        found = await self._call(lambda e: e.get_chargers())
        self._chargers = {c.id: c for c in found}
        return [ChargerRef(id=str(c.id), name=str(c.name)) for c in found]

    async def state(self, charger_id: str) -> ChargerState:
        """What the charger is doing now."""
        raw = await self._observed(charger_id)
        return ChargerState(
            op_mode=_int(raw.get("chargerOpMode")),
            power_kw=_float(raw.get("totalPower")),
            session_kwh=_float(raw.get("sessionEnergy")),
            output_current_a=_float(raw.get("outputCurrent")),
            dynamic_current_a=_float(raw.get("dynamicChargerCurrent")),
            reason_for_no_current=_int(raw.get("reasonForNoCurrent")),
        )

    async def limits(self, charger_id: str) -> Limits:
        """The currents the charger may give, as it reports them."""
        raw = await self._observed(charger_id)
        phases = ("P1", "P2", "P3")
        return Limits(
            max_charger_a=_float(raw.get("maxChargerCurrent")),
            dynamic_charger_a=_float(raw.get("dynamicChargerCurrent")),
            circuit_max_a=_three(raw.get(f"circuitMaxCurrent{p}") for p in phases),
            dynamic_circuit_a=_three(raw.get(f"dynamicCircuitCurrent{p}") for p in phases),
        )

    async def sessions(self, charger_id: str, start: dt.datetime, end: dt.datetime) -> list[Session]:
        """The charging sessions between two times, newest first."""
        charger = await self._charger(charger_id)
        found = await self._call(lambda _: charger.get_sessions_between_dates(start, end))
        return [
            Session(
                connected=s["carConnected"], disconnected=s["carDisconnected"], kwh=float(s["kiloWattHours"])
            )
            for s in found
        ]

    async def energy(self, charger_id: str, start: dt.datetime, end: dt.datetime) -> float:
        """The kWh charged between two times."""
        charger = await self._charger(charger_id)
        return float(await self._call(lambda _: charger.get_consumption_between_dates(start, end)))

    async def close(self) -> None:
        """Close the connection, if one was opened."""
        if self._easee is not None:
            await self._easee.close()
            self._easee = None

    async def _observed(self, charger_id: str) -> dict[str, Any]:
        """The chosen observations, by name; any other Easee sends is dropped."""
        charger = await self._charger(charger_id)
        answer = await self._call(lambda _: charger.get_observations(*OBSERVATIONS.values()))
        by_id = {o.get("id"): o.get("value") for o in answer.get("observations", []) if isinstance(o, dict)}
        return {name: by_id.get(number) for name, number in OBSERVATIONS.items()}

    async def _charger(self, charger_id: str) -> Any:  # noqa: ANN401
        if charger_id not in self._chargers:
            await self.chargers()
        if charger_id not in self._chargers:
            raise CloudDownError
        return self._chargers[charger_id]

    async def _call(self, read: Callable[[Any], Awaitable[Any]]) -> Any:  # noqa: ANN401
        """One read, with Easee's failures turned into the three the voice can say."""
        if self._easee is None:
            user, password = self._login()
            self._easee = pyeasee.Easee(user, password, session=self._session)
        # A 401 makes pyeasee log in again and then raise, so one more try reads
        # with the new token. A second refusal is the login itself.
        for attempt in range(2):
            try:
                async with asyncio.timeout(TIMEOUT):
                    answer = await read(self._easee)
            except exceptions.TooManyRequestsException as slow:
                # RULE: a 429 is never retried
                raise SlowDownError(_wait(slow)) from None
            except (exceptions.AuthorizationFailedException, exceptions.BadRequestException):
                if attempt:
                    raise LoginRefusedError from None
                continue
            except exceptions.ForbiddenServiceException:
                raise NotAllowedError from None
            except Exception:  # noqa: BLE001 - whatever broke, the voice can only say the cloud is down
                raise CloudDownError from None
            if answer is None:  # pyeasee's reads answer None on a server failure
                raise CloudDownError
            return answer
        raise LoginRefusedError  # pragma: no cover - the loop always returns or raises


def _wait(slow: Exception) -> int:
    said = str(slow.args[1]) if len(slow.args) > 1 else ""
    try:
        return max(1, int(said))
    except (TypeError, ValueError):
        return DEFAULT_WAIT


def _float(value: Any) -> float | None:  # noqa: ANN401
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:  # noqa: ANN401
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _three(values: Any) -> tuple[float | None, float | None, float | None]:  # noqa: ANN401
    a, b, c = (_float(v) for v in values)
    return (a, b, c)
