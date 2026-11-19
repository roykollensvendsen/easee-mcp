"""The MCP server: four read-only charger tools, over stdio, for a voice session."""

from __future__ import annotations

import contextlib
import functools
import math
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from easee_mcp.adapter import (
    ChargerGoneError,
    CloudDownError,
    LoginRefusedError,
    NotAllowedError,
    NotSetUpError,
    SlowDownError,
)
from easee_mcp.answers import one_line

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from easee_mcp.answers import ChargerAnswers
    from easee_mcp.status import StatusFile

#: The Claude app shows about this many characters of a tool before loading it.
FIRST_LINE_MAX = 26
INSTRUCTIONS = (
    "The owner's Easee car charger, read from Easee's cloud. Read each 'say' aloud as it is. "
    "It only reads: it never starts, stops or changes the charger, and cannot."
)
SIMULATED = "SIMULATED charger, not the real one: say so. "
#: What the voice hears for each way a read can fail.
DOWN = "Easee's cloud isn't answering; the app or the button on the charger still works."
REFUSED = "Easee refused the login. The saved username or password needs checking."
NOT_ALLOWED = "Easee does not let this login read that."
NOT_SET_UP = "No Easee login is saved, so nothing was asked."
NO_CHARGER = "There is no charger on this Easee account."
BROKE = "That went wrong inside easee-mcp. Try again, and if it fails again, check its log."

STATUS_DESCRIPTION = (
    "Is the car charging?\n"
    "What the charger is doing now: charging or not, the power in kW, the energy this session, "
    "and the current limit. Read at most every 30 seconds; an older answer says its age."
)
CURRENT_DESCRIPTION = (
    "What current may it give?\n"
    "The current the charger may give the car now, what it offers, and the charger's and "
    "the circuit's own maximum, in amperes. It only reads them, never sets them."
)
SESSIONS_DESCRIPTION = (
    "Recent charging sessions.\n"
    "How many sessions in the last 'days' days (1 to 31, default 7), the energy in all, "
    "and when the latest began."
)
ENERGY_DESCRIPTION = (
    "Energy charged lately.\n"
    "The kWh charged in a period: 'period' is 'day' for today, 'week' for this week from "
    "Monday, or 'month' for this month."
)
#: Each failure a read can meet, as the outcome named and the sentence said.
FAILURES: tuple[tuple[type[Exception], tuple[str, str]], ...] = (
    (CloudDownError, ("unreachable", DOWN)),
    (LoginRefusedError, ("login_refused", REFUSED)),
    (NotAllowedError, ("not_allowed", NOT_ALLOWED)),
    (NotSetUpError, ("not_set_up", NOT_SET_UP)),
    (LookupError, ("no_charger", NO_CHARGER)),
)
TOOLS = ("charger_status", "charger_current", "charger_sessions", "charger_energy")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def build_server(
    answers: ChargerAnswers, *, simulated: bool = False, status: StatusFile | None = None
) -> MCPServer[Any]:
    """The MCP server with the four charger tools, answering from ``answers``; ``status`` records each."""

    @contextlib.asynccontextmanager
    async def closing(_: MCPServer[Any]) -> AsyncIterator[None]:
        """Keep one connection to Easee while the server runs, and close it as the server stops."""
        try:
            yield
        finally:
            # RULE: the voice server closes its connection to Easee when it stops
            await answers.close()

    server: MCPServer[Any] = MCPServer(
        name="easee-simulated" if simulated else "easee",
        instructions=(SIMULATED + INSTRUCTIONS) if simulated else INSTRUCTIONS,
        lifespan=closing,
    )

    def recorded(answer: dict[str, Any]) -> None:
        if status is not None:
            status.after(answer, answers.calls_left_hour())

    guarded = _guarded(simulated=simulated, after=recorded)

    @server.tool(annotations=READ_ONLY, description=STATUS_DESCRIPTION)
    @guarded
    async def charger_status() -> dict[str, Any]:
        return await answers.status()

    @server.tool(annotations=READ_ONLY, description=CURRENT_DESCRIPTION)
    @guarded
    async def charger_current() -> dict[str, Any]:
        return await answers.current()

    @server.tool(annotations=READ_ONLY, description=SESSIONS_DESCRIPTION)
    @guarded
    async def charger_sessions(days: int = 7) -> dict[str, Any]:
        return await answers.sessions(days)

    @server.tool(annotations=READ_ONLY, description=ENERGY_DESCRIPTION)
    @guarded
    async def charger_energy(period: str = "day") -> dict[str, Any]:
        return await answers.energy(period)

    return server


def _said(failure: Exception) -> tuple[str, str]:
    """The outcome and the sentence for a failure that is not a wait."""
    if isinstance(failure, ChargerGoneError):
        return "charger_gone", _gone(failure.names)
    return next((said for kind, said in FAILURES if isinstance(failure, kind)), ("broke", BROKE))


def _gone(names: tuple[str, ...]) -> str:
    """Say the charger has left, naming those there now by the names the owner gave them."""
    said = [one_line(n) for n in names]
    there = " and ".join([", ".join(said[:-1]), said[-1]] if len(said) > 1 else said) or "no chargers now"
    return f"That charger is no longer on the Easee account; the account has {there}."


def _failed(outcome: str, say: str) -> dict[str, Any]:
    return {"success": False, "outcome": outcome, "say": say}


def _guarded(
    *, simulated: bool, after: Callable[[dict[str, Any]], None]
) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[dict[str, Any]]]]:
    """Make a tool's failure an answer the voice can still read."""

    def wrap(tool: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
        @functools.wraps(tool)
        async def answered(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            try:
                answer = await tool(*args, **kwargs)
            # RULE: every tool answers with a sentence even when it fails
            except SlowDownError as slow:
                minutes = max(1, math.ceil(slow.seconds / 60))
                plural = "s" if minutes != 1 else ""
                answer = _failed(
                    "slow_down", f"Easee is limiting requests; ask again in {minutes} minute{plural}."
                )
                answer["retry_after_seconds"] = slow.seconds
            except Exception as failure:  # noqa: BLE001 - whatever broke, the voice gets a sentence
                answer = _failed(*_said(failure))
            after(answer)
            answer["simulated"] = simulated
            if simulated:
                answer["say"] = "Simulated: " + answer["say"]
            return answer

        return answered

    return wrap
