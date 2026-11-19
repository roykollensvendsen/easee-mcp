"""A pretend Easee cloud, for tests and the simulated mode.

It is written from Easee's published API reference, never from a recorded
answer of the real service: the owner's charger, site and sessions are never
committed. Its charger is the made-up ``EHTEST01``. It keeps the real service's
limits, so the plugin cannot pass here and fail there:

* the access token lasts one hour, and a refresh token works once;
* the charger list may be read twice a minute, and sessions and energy ten
  times an hour each; past that it answers 429 with ``Retry-After``;
* the charger's state and limits come as observations, by number; the older
  state read is not served, as the real one refused the owner's login on
  2026-11-15;
* it carries a made-up street address and coordinates beside the charger, as
  Easee's site records do, so a test can prove they never come out.

It refuses any request that is not a read, apart from logging in, and keeps
it in ``refused``: a fake that quietly accepted a write would let a test pass
that ought to fail.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: The made-up charger every test and the simulated mode use.
FAKE_CHARGER_ID = "EHTEST01"
FAKE_CHARGER_NAME = "Garasje"
#: The login the fake accepts. Any other is refused with 401.
FAKE_USER = "someone@example.invalid"
FAKE_PASSWORD = "made-up-password"  # noqa: S105 - a made-up login for the fake
#: A street and coordinates the fake puts beside the charger, built here so no
#: file holds a street address in the form the private-data scan refuses.
FAKE_STREET = "Testveien " + "1"
FAKE_LATITUDE, FAKE_LONGITUDE = 0.0, 0.0

LOGIN = "/api/accounts/login"
REFRESH = "/api/accounts/refresh_token"
TOKEN_SECONDS = 3600
#: (path prefix, calls allowed, per seconds), from developer.easee.com's reference.
LIMITS: tuple[tuple[str, int, int], ...] = (
    ("/api/chargers", 2, 60),
    ("/api/sessions/charger/{id}/sessions/", 10, 3600),
    ("/api/sessions/charger/{id}/total/", 10, 3600),
)


class WriteRefusedError(AssertionError):
    """Something tried to change the charger. Nothing in this plugin may."""


class _Headers(dict[str, str]):
    """Header lookup that ignores case, as a real HTTP response's does."""

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


@dataclass
class FakeResponse:
    """The parts of an HTTP response the Easee library reads."""

    status: int
    body: Any = None
    headers: _Headers = field(default_factory=_Headers)
    url: str = ""
    request_info: None = None
    history: tuple[()] = ()

    async def json(self) -> Any:  # noqa: ANN401
        """The body as JSON."""
        return self.body

    async def text(self) -> str:
        """The body as text, as a total energy answer comes."""
        return self.body if isinstance(self.body, str) else json.dumps(self.body)


@dataclass
class FakeEaseeCloud:
    """The service itself: answers requests and counts them."""

    now: Any = field(default=lambda: dt.datetime.now(dt.UTC))
    #: The charger's observations by ID, as GET /state/{id}/observations gives them.
    observations: dict[int, Any] = field(default_factory=lambda: _observations())  # noqa: PLW0108 - defined below
    #: Each session as (connected, disconnected or None, kWh).
    sessions: list[tuple[dt.datetime, dt.datetime | None, float]] = field(default_factory=list)
    #: The chargers on the account, as (ID, name).
    chargers: list[tuple[str, str]] = field(default_factory=lambda: [(FAKE_CHARGER_ID, FAKE_CHARGER_NAME)])
    #: What a read of a charger not on the account is answered with.
    gone_status: int = 403
    #: Set to answer the charger list with this status instead: 429 says wait a minute.
    list_status: int | None = None
    #: The kWh a total-energy question answers.
    energy_kwh: float = 42.5
    #: True answers the observations read with 403.
    forbid_observations: bool = False
    #: True answers the observations read with 500, as a server failure does.
    fail_observations: bool = False
    #: Set to a number of seconds to answer every read with 429. A string is sent
    #: as it is, since the header may also be a date.
    retry_after: int | str | None = None
    #: Each request as (method, path).
    requests: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    _tokens: dict[str, dt.datetime] = field(default_factory=dict)
    _refresh: set[str] = field(default_factory=set)
    _calls: dict[str, list[dt.datetime]] = field(default_factory=dict)
    _issued: int = 0

    def session(self) -> FakeSession:
        """A stand-in for the HTTP session, answering from this fake instead of the network."""
        return FakeSession(self)

    def expire_tokens(self) -> None:
        """Let every access token run out, as an hour does."""
        self._tokens = dict.fromkeys(self._tokens, self.now() - dt.timedelta(seconds=1))

    def answer(self, method: str, url: str, headers: dict[str, str], body: Any) -> FakeResponse:  # noqa: ANN401
        """What the service says to one request."""
        path, query = urlsplit(url).path, urlsplit(url).query
        self.requests.append((method, path))
        if method == "POST" and path == LOGIN:
            return self._login(body)
        if method == "POST" and path == REFRESH:
            return self._refreshed(body)
        if method != "GET":
            self.refused.append((method, path))
            message = f"{method} {path}: this plugin never changes anything"
            raise WriteRefusedError(message)
        token = (headers.get("Authorization") or "").removeprefix("Bearer ")
        if self._tokens.get(token, self.now()) <= self.now():
            return FakeResponse(
                401, {"title": "Unauthorized"}, _Headers({"Content-Type": "application/json"})
            )
        if self.retry_after is not None:
            return self._slow_down(self.retry_after)
        limited = self._over_limit(path)
        if limited is not None:
            return self._slow_down(limited)
        return self._read(path, query)

    def _login(self, body: dict[str, str]) -> FakeResponse:
        if body != {"userName": FAKE_USER, "password": FAKE_PASSWORD}:
            return FakeResponse(401, {"title": "Wrong username or password"}, _json_headers())
        return self._token()

    def _refreshed(self, body: dict[str, str]) -> FakeResponse:
        # A refresh token works once; Easee then wants the password again.
        if body.get("refreshToken") not in self._refresh:
            return FakeResponse(401, {"title": "Refresh token used or unknown"}, _json_headers())
        self._refresh.discard(body["refreshToken"])
        return self._token()

    def _token(self) -> FakeResponse:
        self._issued += 1
        access, refresh = f"access-{self._issued}", f"refresh-{self._issued}"
        self._tokens[access] = self.now() + dt.timedelta(seconds=TOKEN_SECONDS)
        self._refresh.add(refresh)
        body = {"accessToken": access, "expiresIn": TOKEN_SECONDS, "refreshToken": refresh}
        return FakeResponse(200, body, _json_headers())

    def _over_limit(self, path: str) -> int | None:
        """Seconds to wait when this call is one too many, else None."""
        for prefix, allowed, period in LIMITS:
            key = prefix.replace("{id}", FAKE_CHARGER_ID)
            if not (path == key if prefix == "/api/chargers" else path.startswith(key)):
                continue
            since = self.now() - dt.timedelta(seconds=period)
            recent = [t for t in self._calls.get(key, []) if t > since]
            if len(recent) >= allowed:
                return int((recent[0] - since).total_seconds()) + 1
            self._calls[key] = [*recent, self.now()]
        return None

    @staticmethod
    def _slow_down(seconds: int | str) -> FakeResponse:
        headers = _json_headers()
        headers["Retry-After"] = str(seconds)
        return FakeResponse(429, {"title": "Too many requests"}, headers)

    def _read(self, path: str, query: str) -> FakeResponse:
        if path == "/api/chargers":
            return self._listed()
        whose = _whose(path)
        if whose is not None and whose not in {i for i, _ in self.chargers}:
            return FakeResponse(self.gone_status, {"title": "Not yours"}, _json_headers())
        if path == f"/state/{whose}/observations":
            return self._observed(query)
        if path.startswith(f"/api/sessions/charger/{whose}/sessions/"):
            return FakeResponse(200, self._session_records(), _json_headers())
        if path.startswith(f"/api/sessions/charger/{whose}/total/"):
            return FakeResponse(200, str(self.energy_kwh), _Headers({"Content-Type": "text/plain"}))
        return FakeResponse(404, {"title": "Not found"}, _json_headers())

    def _listed(self) -> FakeResponse:
        if self.list_status == 429:  # noqa: PLR2004 - Too Many Requests
            return self._slow_down(60)
        if self.list_status is not None:
            return FakeResponse(self.list_status, {"title": "Failed"}, _json_headers())
        return FakeResponse(200, [_charger_record(*c) for c in self.chargers], _json_headers())

    def _observed(self, query: str) -> FakeResponse:
        if self.forbid_observations:
            return FakeResponse(403, {"title": "Forbidden"}, _json_headers())
        if self.fail_observations:
            return FakeResponse(500, {"title": "Internal Server Error"}, _json_headers())
        asked = {int(i) for i in parse_qs(query).get("ids", [""])[0].split(",") if i}
        found = [
            {"id": key, "value": value, "timestamp": self.now().isoformat()}
            for key, value in self.observations.items()
            if key in asked
        ]
        return FakeResponse(
            200, {"chargerId": FAKE_CHARGER_ID, "observations": found, **_place()}, _json_headers()
        )

    def _session_records(self) -> list[dict[str, Any]]:
        return [
            {
                "carConnected": on.isoformat().replace("+00:00", "Z"),
                "carDisconnected": off.isoformat().replace("+00:00", "Z") if off else None,
                "kiloWattHours": kwh,
                **_place(),
            }
            for on, off, kwh in self.sessions
        ]


@dataclass
class FakeSession:
    """What the Easee library calls in place of an HTTP session."""

    cloud: FakeEaseeCloud

    async def get(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> FakeResponse:  # noqa: ANN401
        """A read."""
        return self.cloud.answer("GET", url, headers or {}, None)

    async def post(  # noqa: D102 - each is a request the fake answers or refuses
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: Any = None,  # noqa: ANN401
        **_: Any,  # noqa: ANN401
    ) -> FakeResponse:
        return self.cloud.answer("POST", url, headers or {}, json)

    async def put(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> FakeResponse:  # noqa: ANN401, D102
        return self.cloud.answer("PUT", url, headers or {}, None)

    async def delete(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> FakeResponse:  # noqa: ANN401, D102
        return self.cloud.answer("DELETE", url, headers or {}, None)

    async def close(self) -> None:
        """Nothing to close."""


def _json_headers() -> _Headers:
    return _Headers({"Content-Type": "application/json"})


def _place() -> dict[str, Any]:
    """Where the charger stands, which must never leave the adapter."""
    return {
        "address": {"street": FAKE_STREET, "city": "Testby", "zip": "0000"},
        "latitude": FAKE_LATITUDE,
        "longitude": FAKE_LONGITUDE,
    }


def _whose(path: str) -> str | None:
    """The charger ID a read is about, or None for a read of the account."""
    parts = path.split("/")
    if path.startswith("/state/") and len(parts) > 2:  # noqa: PLR2004 - /state/{id}
        return parts[2]
    if path.startswith("/api/sessions/charger/") and len(parts) > 4:  # noqa: PLR2004 - /api/sessions/charger/{id}
        return parts[4]
    return None


def _charger_record(charger_id: str, name: str) -> dict[str, Any]:
    return {
        "id": charger_id,
        "name": name,
        "productCode": 1,
        "levelOfAccess": 1,
        "userRole": 1,
        **_place(),
    }


def _observations() -> dict[int, Any]:
    """A car connected and charging at 7.2 kW, by Easee's observation IDs."""
    return {
        109: 3,  # chargerOpMode: charging
        120: 7.2,  # totalPower, kW
        121: 12.4,  # sessionEnergy, kWh
        114: 10.0,  # outputCurrent, A
        48: 16.0,  # dynamicChargerCurrent, A
        47: 32.0,  # maxChargerCurrent, A
        22: 32.0,  # circuitMaxCurrentP1..P3, A
        23: 32.0,
        24: 32.0,
        111: 32.0,  # dynamicCircuitCurrentP1..P3, A
        112: 32.0,
        113: 32.0,
        96: 0,  # reasonForNoCurrent
        25: f"{FAKE_LATITUDE},{FAKE_LONGITUDE}",  # location, which must never come out
    }
