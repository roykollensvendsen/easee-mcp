"""The adapter reads the charger through pyeasee, and lets nothing else out."""

import asyncio
import datetime as dt
import logging

import pyeasee
import pytest

from easee_mcp.adapter import (
    DEFAULT_WAIT,
    ChargerRef,
    CloudDownError,
    EaseeReader,
    LoginRefusedError,
    NotAllowedError,
    NotFoundError,
    SlowDownError,
    mask,
)
from easee_mcp.fake import (
    FAKE_CHARGER_ID,
    FAKE_PASSWORD,
    FAKE_STREET,
    FAKE_USER,
    FakeEaseeCloud,
)

UTC = dt.UTC
NOV = dt.datetime(2026, 11, 14, tzinfo=UTC)


def _reader(cloud, *, user=FAKE_USER, password=FAKE_PASSWORD):
    return EaseeReader(lambda: (user, password), session=cloud.session())


def _run(coroutine):
    return asyncio.run(coroutine)


async def _with(reader, call):
    try:
        return await call(reader)
    finally:
        await reader.close()


def test_the_chargers_on_the_account_are_found_at_run_time():
    cloud = FakeEaseeCloud()
    found = _run(_with(_reader(cloud), lambda r: r.chargers()))
    assert found == [ChargerRef(id=FAKE_CHARGER_ID, name="Garasje")]


def test_the_state_says_what_the_charger_is_doing():
    cloud = FakeEaseeCloud()
    state = _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))
    assert state.op_mode == 3
    assert (state.power_kw, state.session_kwh) == (7.2, 12.4)
    assert (state.output_current_a, state.dynamic_current_a) == (10.0, 16.0)


def test_the_limits_are_read_and_never_set():
    cloud = FakeEaseeCloud()
    limits = _run(_with(_reader(cloud), lambda r: r.limits(FAKE_CHARGER_ID)))
    assert limits.max_charger_a == 32.0
    assert limits.dynamic_charger_a == 16.0
    assert limits.circuit_max_a == (32.0, 32.0, 32.0)
    assert limits.dynamic_circuit_a == (32.0, 32.0, 32.0)
    assert {method for method, _ in cloud.requests} <= {"GET", "POST"}


def test_sessions_come_back_newest_first_with_their_energy():
    cloud = FakeEaseeCloud()
    cloud.sessions = [
        (NOV.replace(hour=17), NOV.replace(hour=23), 18.5),
        (NOV.replace(day=15, hour=8), None, 3.0),
    ]
    sessions = _run(_with(_reader(cloud), lambda r: r.sessions(FAKE_CHARGER_ID, NOV, NOV.replace(day=16))))
    assert [s.kwh for s in sessions] == [3.0, 18.5]
    assert sessions[0].disconnected is None
    assert sessions[1].connected == NOV.replace(hour=17)


def test_the_energy_between_two_days_is_read():
    cloud = FakeEaseeCloud()
    kwh = _run(_with(_reader(cloud), lambda r: r.energy(FAKE_CHARGER_ID, NOV, NOV.replace(day=15))))
    assert kwh == 42.5


def test_a_wrong_login_is_said_as_refused():
    cloud = FakeEaseeCloud()
    wrong = "not-" + FAKE_PASSWORD
    with pytest.raises(LoginRefusedError):
        _run(_with(_reader(cloud, password=wrong), lambda r: r.chargers()))


def test_a_429_is_said_with_the_wait_easee_gives():
    cloud = FakeEaseeCloud(retry_after=120)
    with pytest.raises(SlowDownError) as raised:
        _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))
    assert raised.value.seconds == 120


def test_a_429_is_never_retried():
    cloud = FakeEaseeCloud(retry_after=60)
    with pytest.raises(SlowDownError):
        _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))
    assert len([p for m, p in cloud.requests if m == "GET"]) == 1


def test_the_state_and_limits_come_from_one_read_of_the_observations():
    cloud = FakeEaseeCloud()

    async def both(reader):
        await reader.chargers()
        await reader.limits(FAKE_CHARGER_ID)

    _run(_with(_reader(cloud), both))
    reads = [p for m, p in cloud.requests if m == "GET"]
    assert reads == ["/api/chargers", f"/state/{FAKE_CHARGER_ID}/observations"]


def test_a_read_easee_forbids_is_said_as_not_allowed():
    cloud = FakeEaseeCloud()
    cloud.observations = {}
    cloud.forbid_observations = True
    with pytest.raises(NotAllowedError):
        _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))


def test_a_cloud_that_cannot_be_reached_is_said_as_down():
    class Broken:
        async def post(self, *_, **__):
            raise OSError

        async def close(self):
            pass

    reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=Broken())
    with pytest.raises(CloudDownError):
        _run(_with(reader, lambda r: r.chargers()))


def test_a_server_failure_is_said_as_down():
    """On this read a 500 comes back from pyeasee as None, not as an error."""
    cloud = FakeEaseeCloud()
    cloud.fail_observations = True
    with pytest.raises(CloudDownError):
        _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))


def test_a_charger_this_reader_never_listed_is_not_found():
    """The cache then lists the chargers again and says the charger is gone."""
    cloud = FakeEaseeCloud()
    with pytest.raises(NotFoundError):
        _run(_with(_reader(cloud), lambda r: r.state("not-" + FAKE_CHARGER_ID)))


def test_a_wait_given_as_a_date_is_said_as_the_usual_wait():
    """Retry-After may be a date instead of seconds; the usual wait is said then."""
    cloud = FakeEaseeCloud(retry_after="Thu, 19 Nov 2026 07:30:00 GMT")
    with pytest.raises(SlowDownError) as raised:
        _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))
    assert raised.value.seconds == DEFAULT_WAIT


def test_a_value_that_is_not_a_number_is_read_as_unknown():
    cloud = FakeEaseeCloud()
    cloud.observations |= {109: "charging", 120: "n/a"}
    state = _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))
    assert (state.op_mode, state.power_kw) == (None, None)
    assert state.session_kwh == 12.4


@pytest.mark.parametrize(
    ("theirs", "ours", "status"),
    [("ForbiddenServiceException", NotAllowedError, 403), ("NotFoundException", NotFoundError, 404)],
)
def test_a_narrower_refusal_from_pyeasee_is_still_said_as_that_refusal(monkeypatch, theirs, ours, status):
    """A later pyeasee may raise a subclass; it must not fall through to "broke"."""
    narrower = type("Narrower", (getattr(pyeasee.exceptions, theirs),), {})
    monkeypatch.setattr(pyeasee.easee, theirs, narrower)
    cloud = FakeEaseeCloud(gone_status=status)

    async def after_it_leaves(reader):
        await reader.chargers()
        cloud.chargers = []
        return await reader.state(FAKE_CHARGER_ID)

    with pytest.raises(ours):
        _run(_with(_reader(cloud), after_it_leaves))


def test_an_expired_token_logs_in_again_and_reads():
    cloud = FakeEaseeCloud()

    async def twice(reader):
        await reader.state(FAKE_CHARGER_ID)
        cloud.expire_tokens()
        return await reader.state(FAKE_CHARGER_ID)

    assert _run(_with(_reader(cloud), twice)).op_mode == 3


def test_the_home_address_never_leaves_the_adapter():
    cloud = FakeEaseeCloud()
    cloud.sessions = [(NOV, None, 1.0)]

    async def everything(reader):
        return [
            await reader.chargers(),
            await reader.state(FAKE_CHARGER_ID),
            await reader.limits(FAKE_CHARGER_ID),
            await reader.sessions(FAKE_CHARGER_ID, NOV, NOV),
        ]

    out = repr(_run(_with(_reader(cloud), everything)))
    for word in (FAKE_STREET, "Testby", "address", "latitude", "longitude", "0.0,0.0"):
        assert word not in out


def test_the_library_never_logs(caplog):
    cloud = FakeEaseeCloud()
    with caplog.at_level(logging.DEBUG):
        _run(_with(_reader(cloud), lambda r: r.state(FAKE_CHARGER_ID)))
    assert [r for r in caplog.records if r.name.startswith(("pyeasee", "pysignalr"))] == []
    assert FAKE_USER not in caplog.text
    assert FAKE_CHARGER_ID not in caplog.text


def test_a_charger_id_is_masked_wherever_it_is_shown():
    assert mask(FAKE_CHARGER_ID) == "EH…01"
    assert mask("") == "…"
