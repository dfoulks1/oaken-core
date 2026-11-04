# SPDX-License-Identifier: Apache-2.0
"""Serve the API on a socket (uvicorn). The authenticated door binds loopback unless ``remote_ok``.

Adapted from oaken-mail ``api/server.py`` (cf3b187). The console is a separate, always-loopback
listener (step 6) and is not governed by ``remote_ok``.
"""

import ipaddress

import uvicorn

from oaken_core.api.app import Api
from oaken_core.errors import ConfigError
from oaken_core.settings import ApiSettings


def check_bind(cfg: ApiSettings) -> None:
    """Refuse a non-loopback host unless ``remote_ok``: past loopback the bearer token is the only
    gate, so the owner says so on purpose."""
    host = cfg.host.strip().lower()
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not cfg.remote_ok:
        raise ConfigError(
            f"[api] host {cfg.host!r} is not loopback; set remote_ok = true to serve beyond this machine"
        )


class ApiServer:
    def __init__(self, api: Api) -> None:
        self.api = api
        cfg = api.cfg
        check_bind(cfg)
        self._server = uvicorn.Server(
            uvicorn.Config(api, host=cfg.host, port=cfg.port, log_level="warning", lifespan="off", access_log=False)
        )

    @property
    def port(self) -> int:
        for server in self._server.servers:
            for sock in server.sockets:
                return int(sock.getsockname()[1])
        return self.api.cfg.port

    async def serve(self) -> None:
        """Run until ``stop`` is called. ``started`` is true once the socket is bound."""
        await self._server.serve()

    @property
    def started(self) -> bool:
        return bool(self._server.started)

    def stop(self) -> None:
        self._server.should_exit = True
