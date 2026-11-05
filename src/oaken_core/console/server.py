# SPDX-License-Identifier: Apache-2.0
"""The console's listener. ``HOST`` is a constant: no setting, flag or environment variable can
change it (``ConsoleSettings`` has no host field and forbids extras)."""

import uvicorn

from oaken_core.console.app import Console
from oaken_core.settings import ConsoleSettings

HOST = "127.0.0.1"


class ConsoleServer:
    def __init__(self, console: Console, cfg: ConsoleSettings) -> None:
        self.console, self.cfg = console, cfg
        self._server = uvicorn.Server(
            uvicorn.Config(console, host=HOST, port=cfg.port, log_level="warning", lifespan="off", access_log=False)
        )

    @property
    def port(self) -> int:
        for server in self._server.servers:
            for sock in server.sockets:
                return int(sock.getsockname()[1])
        return self.cfg.port

    @property
    def started(self) -> bool:
        return bool(self._server.started)

    async def serve(self) -> None:
        await self._server.serve()

    def stop(self) -> None:
        self._server.should_exit = True
