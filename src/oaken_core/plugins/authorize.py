# SPDX-License-Identifier: Apache-2.0
"""Interactive authorization that outlives one request.

Gmail's consent flow opens a loopback port, waits for Google to redirect the person's browser to it,
and then exchanges the code. A client cannot hold a request open for that, so core starts it as a
background task, hands back the address to open, and answers "how is it going" until it is done.
The refresh token goes from the provider straight to the OS keyring; nothing here ever sees it.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Literal

from oaken_core.errors import Conflict, OakenError
from oaken_core.logging import get_logger

log = get_logger("oaken_core.authorize")
State = Literal["none", "pending", "done", "failed"]


@dataclass
class _Run:
    url: str = ""
    state: State = "pending"
    detail: str = ""
    error: Exception | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class Authorizations:
    """At most one authorization per account at a time."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}

    async def start(self, account_id: str, provider: Any, *, wait_for_url: float = 30.0) -> str:
        """Begin ``provider.authorize`` and return the address the person must open."""
        current = self._runs.get(account_id)
        if current is not None and current.state == "pending":
            if current.url:
                return current.url
            raise Conflict(f"an authorization for {account_id!r} is already starting")
        run = _Run()
        self._runs[account_id] = run
        shown: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def show(url: str) -> None:
            run.url = url
            if not shown.done():
                shown.set_result(url)

        async def go() -> None:
            try:
                await provider.authorize(show)
                run.state = "done"
                log.info("account authorized", account_id=account_id)
            except asyncio.CancelledError:
                run.state, run.detail = "failed", "core stopped before it finished"
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the person, never raised here
                run.state = "failed"
                run.error = exc
                run.detail = exc.message if isinstance(exc, OakenError) else type(exc).__name__
                log.warning("authorization failed", account_id=account_id, error=type(exc).__name__)

        run.task = asyncio.create_task(go())
        waiting: set[asyncio.Future[Any]] = {shown, run.task}
        await asyncio.wait(waiting, timeout=wait_for_url, return_when=asyncio.FIRST_COMPLETED)
        if shown.done() and not shown.cancelled():
            return shown.result()
        run.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run.task
        if run.error is not None:
            raise run.error
        raise Conflict("the provider did not offer an authorization address")

    def status(self, account_id: str) -> tuple[State, str]:
        run = self._runs.get(account_id)
        return ("none", "") if run is None else (run.state, run.detail)

    async def close(self) -> None:
        for run in self._runs.values():
            if run.task is not None and not run.task.done():
                run.task.cancel()
        for run in self._runs.values():
            if run.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run.task
