"""What the voice says about the charger, in one or two sentences."""

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from easee_mcp.adapter import EaseeReader
from easee_mcp.answers import ChargerAnswers, one_line
from easee_mcp.cache import CachedCharger
from easee_mcp.fake import FAKE_CHARGER_ID, FAKE_PASSWORD, FAKE_USER, FakeEaseeCloud

OSLO = ZoneInfo("Europe/Oslo")
AT = dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO)


@pytest.fixture
def cloud():
    return FakeEaseeCloud(now=lambda: AT)


def _ask(cloud, call, now=lambda: AT):
    reader = EaseeReader(lambda: (FAKE_USER, FAKE_PASSWORD), session=cloud.session())
    cached = CachedCharger(reader, now=now)

    async def asked():
        try:
            return await call(ChargerAnswers(cached, tz=OSLO))
        finally:
            await cached.close()

    return asyncio.run(asked())


def test_charging_is_said_with_the_power_and_the_session_energy(cloud):
    answer = _ask(cloud, lambda a: a.status())
    assert answer["say"] == "Garasje is charging at 7.2 kW, 12.4 kWh this session. It is limited to 16 A."
    assert answer["charging"] is True
    assert answer["success"] is True


def test_a_figure_easee_does_not_send_is_left_out_never_said_as_zero(cloud):
    for missing in (120, 121):  # totalPower and sessionEnergy, absent when not charging
        del cloud.observations[missing]
    cloud.observations[109] = 1
    answer = _ask(cloud, lambda a: a.status())
    assert answer["say"] == "Garasje has no car connected. It is limited to 16 A."
    assert answer["charging"] is False
    assert answer["power_kw"] is None


def test_an_unknown_state_is_said_as_unknown(cloud):
    cloud.observations[109] = 77
    assert _ask(cloud, lambda a: a.status())["say"].startswith("Garasje reports a state I don't know (77)")


def test_no_limit_is_said_when_the_charger_may_give_its_maximum(cloud):
    cloud.observations[48] = 32
    assert "limited" not in _ask(cloud, lambda a: a.status())["say"]


def test_the_current_says_what_the_charger_may_give_and_why(cloud):
    answer = _ask(cloud, lambda a: a.current())
    assert answer["say"] == (
        "Garasje may give the car up to 16 A now, and offers it 10 A. "
        "The charger's own maximum is 32 A, and the circuit's is 32 A."
    )
    assert answer["dynamic_charger_a"] == 16.0


def test_sessions_are_counted_and_the_latest_is_told(cloud):
    cloud.sessions = [
        (
            dt.datetime(2026, 11, 12, 16, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 11, 12, 22, 0, tzinfo=dt.UTC),
            18.5,
        ),
        (
            dt.datetime(2026, 11, 13, 17, 5, tzinfo=dt.UTC),
            dt.datetime(2026, 11, 13, 20, 0, tzinfo=dt.UTC),
            9.25,
        ),
    ]
    answer = _ask(cloud, lambda a: a.sessions(days=7))
    assert answer["say"] == (
        "2 charging sessions in the last 7 days, 27.8 kWh in all. "
        "The latest began Friday at 18:05 and gave 9.2 kWh."
    )
    assert answer["sessions"] == 2


def test_no_sessions_are_said_as_none(cloud):
    assert _ask(cloud, lambda a: a.sessions(days=3))["say"] == "No charging sessions in the last 3 days."


def test_the_days_asked_for_are_kept_between_one_and_thirty_one(cloud):
    assert _ask(cloud, lambda a: a.sessions(days=400))["say"].endswith("in the last 31 days.")


def test_the_energy_is_said_for_the_period_asked(cloud):
    assert _ask(cloud, lambda a: a.energy("week"))["say"] == "42.5 kWh charged this week."
    assert _ask(cloud, lambda a: a.energy("day"))["say"] == "42.5 kWh charged today."


def test_an_unknown_period_is_asked_about_never_guessed(cloud):
    answer = _ask(cloud, lambda a: a.energy("fortnight"))
    assert answer["outcome"] == "which_period"
    assert answer["say"] == "Which period: today, this week or this month?"
    assert not [p for _, p in cloud.requests if "/total/" in p]


def test_an_old_answer_says_its_age(cloud):
    clock = {"at": AT}

    async def twice(a):
        await a.status()
        clock["at"] = AT + dt.timedelta(minutes=3)
        cloud.retry_after = 60
        return await a.status()

    answer = _ask(cloud, twice, now=lambda: clock["at"])
    assert answer["say"].endswith(" As of 3 minutes ago.")
    assert answer["age_seconds"] == 180


def test_a_charger_name_is_said_as_one_short_line():
    assert one_line("Gar\nasje\x07  med   tak") == "Gar asje med tak"
    assert len(one_line("x" * 200)) == 40
    assert one_line(" \n ") == "The charger"


def test_no_answer_holds_the_charger_id():
    for call in (lambda a: a.status(), lambda a: a.current(), lambda a: a.energy("month")):
        # A cloud each, as the charger list may be read only twice a minute.
        assert FAKE_CHARGER_ID not in repr(_ask(FakeEaseeCloud(now=lambda: AT), call))
