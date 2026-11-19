"""The cache: a chatty voice never costs Easee more than its limits allow."""

import asyncio
import datetime as dt

import pytest

from easee_mcp.adapter import ChargerGoneError, EaseeReader, NotAllowedError, SlowDownError
from easee_mcp.cache import CachedCharger
from easee_mcp.fake import FAKE_PASSWORD, FAKE_USER, FakeEaseeCloud

AT = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self):
        self.at = AT

    def __call__(self):
        return self.at

    def tick(self, seconds):
        self.at += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cloud(clock):
    return FakeEaseeCloud(now=clock)


def _charger(cloud, clock):
    reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=cloud.session())
    return CachedCharger(reader, now=clock)


def _gets(cloud, part):
    return len([p for m, p in cloud.requests if m == "GET" and part in p])


def _run(charger, *calls):
    async def all_of():
        try:
            return [await call(charger) for call in calls]
        finally:
            await charger.close()

    return asyncio.run(all_of())


def test_the_state_is_read_at_most_every_thirty_seconds(cloud, clock):
    def tick(seconds):
        async def later(_):
            clock.tick(seconds)

        return later

    def status(c):
        return c.reading()

    charger = _charger(cloud, clock)
    _run(charger, status, tick(10), status, tick(19), status)
    assert _gets(cloud, "/observations") == 1
    charger = _charger(cloud, clock)
    _run(charger, status, tick(31), status)
    assert _gets(cloud, "/observations") == 3


def test_the_charger_list_is_read_once_for_the_process(cloud, clock):
    charger = _charger(cloud, clock)
    _run(charger, lambda c: c.reading(), lambda c: c.sessions(days=7), lambda c: c.energy("day"))
    assert _gets(cloud, "/api/chargers") == 1


def test_a_reading_says_how_old_it_is(cloud, clock):
    async def later(c):
        clock.tick(20)
        return await c.reading()

    first, second = _run(_charger(cloud, clock), lambda c: c.reading(), later)
    assert first.age_seconds == 0
    assert second.age_seconds == 20


def test_sessions_are_read_at_most_ten_times_an_hour(cloud, clock):
    def asked(days):
        async def ask(c):
            clock.tick(60)
            return await c.sessions(days=days)  # a new question, so the cache cannot answer it

        return ask

    charger = _charger(cloud, clock)
    _run(charger, *[asked(days) for days in range(1, 11)])
    with pytest.raises(SlowDownError):
        _run(charger, asked(11))
    assert _gets(cloud, "/sessions/") == 10


def test_sessions_are_kept_for_ten_minutes(cloud, clock):
    async def later(c):
        clock.tick(599)
        return await c.sessions(days=7)

    _run(_charger(cloud, clock), lambda c: c.sessions(days=7), later)
    assert _gets(cloud, "/sessions/") == 1


def test_a_429_stops_calls_until_retry_after_passes(cloud, clock):
    cloud.retry_after = 300
    charger = _charger(cloud, clock)
    with pytest.raises(SlowDownError) as first:
        _run(charger, lambda c: c.reading())
    asked = len(cloud.requests)
    cloud.retry_after = None
    clock.tick(299)
    with pytest.raises(SlowDownError) as second:
        _run(charger, lambda c: c.reading())
    assert len(cloud.requests) == asked  # nothing was sent while Easee said wait
    assert (first.value.seconds, second.value.seconds) == (300, 1)
    clock.tick(2)
    assert _run(charger, lambda c: c.reading())[0].state.op_mode == 3


def test_an_old_answer_is_given_while_easee_says_wait(cloud, clock):
    charger = _charger(cloud, clock)
    _run(charger, lambda c: c.reading())
    clock.tick(40)
    cloud.retry_after = 120
    stale = _run(charger, lambda c: c.reading())[0]
    assert stale.state.op_mode == 3
    assert stale.age_seconds == 40


def test_the_calls_left_this_hour_are_counted(cloud, clock):
    charger = _charger(cloud, clock)
    assert charger.calls_left_hour() == 10
    _run(charger, lambda c: c.sessions(days=7))
    assert charger.calls_left_hour() == 9
    _run(charger, lambda c: c.energy("week"), lambda c: c.energy("month"))
    assert charger.calls_left_hour() == 8  # the tighter of the two hourly limits


ELSEWHERE = ("EHTEST02", "Charger B")
READS = {
    "reading": lambda c: c.reading(),
    "sessions": lambda c: c.sessions(days=7),
    "energy": lambda c: c.energy("day"),
}


def _leaves(cloud, *, refused_with=403, now_there=(ELSEWHERE,)):
    cloud.chargers = list(now_there)
    cloud.gone_status = refused_with


@pytest.mark.parametrize("refused_with", [403, 404])
@pytest.mark.parametrize("read", READS)
def test_a_charger_that_left_the_account_is_said_as_gone(cloud, clock, read, refused_with):
    async def after_it_leaves(charger):
        await charger.which()
        _leaves(cloud, refused_with=refused_with)
        return await READS[read](charger)

    with pytest.raises(ChargerGoneError) as gone:
        _run(_charger(cloud, clock), after_it_leaves)
    assert gone.value.names == ("Charger B",)
    assert _gets(cloud, "/api/chargers") == 2


def test_a_refused_charger_still_on_the_account_keeps_its_refusal(cloud, clock):
    cloud.forbid_observations = True
    with pytest.raises(NotAllowedError):
        _run(_charger(cloud, clock), lambda c: c.reading())
    assert _gets(cloud, "/api/chargers") == 2


def test_a_charger_list_easee_limits_after_a_refusal_says_wait_once(cloud, clock):
    async def after_it_leaves(charger):
        await charger.which()
        _leaves(cloud)
        cloud.list_status = 429
        return await charger.reading()

    with pytest.raises(SlowDownError):
        _run(_charger(cloud, clock), after_it_leaves)
    assert _gets(cloud, "/api/chargers") == 2


def test_a_charger_list_that_fails_after_a_refusal_keeps_the_refusal(cloud, clock):
    async def after_it_leaves(charger):
        await charger.which()
        _leaves(cloud)
        cloud.list_status = 500
        return await charger.reading()

    with pytest.raises(NotAllowedError):
        _run(_charger(cloud, clock), after_it_leaves)
    assert _gets(cloud, "/api/chargers") == 2


def test_after_a_charger_leaves_nothing_kept_from_it_is_given(cloud, clock):
    async def before_and_after(charger):
        await charger.sessions(days=7)  # kept ten minutes
        clock.tick(31)
        _leaves(cloud)
        with pytest.raises(ChargerGoneError):
            await charger.reading()
        return await charger.which(), await charger.sessions(days=7)

    [(now_used, again)] = _run(_charger(cloud, clock), before_and_after)
    assert now_used.name == "Charger B"
    assert again.age_seconds == 0
    assert _gets(cloud, "/sessions/") == 2


def test_a_refusal_is_never_said_as_a_wait_of_our_own_making(cloud, clock):
    cloud.forbid_observations = True

    async def twice(charger):
        for _ in range(2):
            with pytest.raises(NotAllowedError):
                await charger.reading()

    _run(_charger(cloud, clock), twice)
    assert _gets(cloud, "/api/chargers") == 2
