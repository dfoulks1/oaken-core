# SPDX-License-Identifier: Apache-2.0
"""``socket``: the machine's door. Send things to the socket and they are submitted to Intake.

It listens on a Unix socket (mode 0600, the default way) or on a TCP port of 127.0.0.1, never on another
address. Whatever arrives becomes an item in intake. Two framings:

- ``lines`` (default): every line is one item. The reply to each is ``OK <subject id>`` or ``ERR <why>``.
- ``connection``: everything up to the sender closing its end is one item, and the reply is the same one line.

    printf 'one\\ntwo\\n' | nc -U /run/user/1000/oaken.sock
    cat fax.txt | nc -N 127.0.0.1 7777        # framing = "connection"

A body is the parsed JSON (``parse_json``, on by default). In ``lines`` framing the sender may use JSON Lines or
one JSON document, compact or pretty-printed over many lines: a value is complete when it parses, and each one is
answered. A top-level array is one item per element. Text that is not JSON gets ``ERR not JSON`` and the connection
stays open. With ``connection`` framing the whole connection is read first, as one document or JSON Lines, all or
nothing. ``parse_json = false`` submits the text as UTF-8 instead. There is no login: the Unix socket's file mode is the guard, and a TCP port is open to every local
user, so use TCP only where that is acceptable.

shortcut: no cap on how many connections are open at once, fine for a loopback listener you own; add one before
pointing anything untrusted at it.
"""

import asyncio
import contextlib
import errno
import json
import os
import stat
from typing import Any, ClassVar, Literal

from oaken_core.errors import PluginError
from oaken_core.plugins.base import BaseProvider, BaseTemplatePlugin
from oaken_core.plugins.contracts import Health
from pydantic import BaseModel, ConfigDict, Field, model_validator

LOOPBACK = "127.0.0.1"  # hard-coded: this plugin never listens beyond this machine


class SocketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unix_path: str = Field(
        default="",
        description="A Unix socket to listen on (made with mode 0600). Set this or port.",
    )
    port: int | None = Field(
        default=None,
        ge=0,
        le=65535,
        description="A TCP port on 127.0.0.1. 0 picks a free one. Set this or unix_path.",
    )
    source: str = Field(
        default="",
        description="What the record calls where these came from. Empty: this plugin's name.",
    )
    format: str = Field(
        default="",
        description="What the items are (json, ini, a format a formatter plugin declares): it splits each into parts before triage. Empty: a JSON object is split by key, anything else stays whole.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description='Labels set on every item from this door, before triage (confidential = "true" keeps them off hosted models).',
    )
    framing: Literal["lines", "connection"] = Field(
        default="lines", description="One item per line, or one item per connection."
    )
    parse_json: bool = Field(
        default=True,
        description="Submit each item as parsed JSON, and answer ERR to anything that is not JSON. False: submit the text as it is.",
    )
    max_item_bytes: int = Field(
        default=1_048_576,
        ge=1,
        description="The biggest item taken. A bigger one gets ERR and the connection closes.",
    )

    @model_validator(mode="after")
    def _one_place(self) -> "SocketConfig":
        if bool(self.unix_path) == (self.port is not None):
            raise ValueError("set exactly one of unix_path or port")
        return self


_DECODER = json.JSONDecoder()


def _records(text: str, *, whole: bool) -> list[Any] | None:
    """The items in JSON text, or None when it is not JSON. One value is one item; an array is one item per element
    (a JSON array and JSON Lines are the same records, the array with one more load step). A whole connection that
    is not a single JSON document may be JSON Lines: one item per non-blank line."""
    try:
        value = json.loads(text)
    except ValueError:
        if not whole:
            return None
        try:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            return None
    return value if isinstance(value, list) else [value]


class SocketProvider(BaseTemplatePlugin, BaseProvider):
    """A door for the machine: whatever is written to its Unix socket (or loopback port) is submitted to intake. A template: make an
    instance per door, each with its own path or port, source and format."""
    icon = "bi-plug"  # a socket door

    api_version = (
        1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    )
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "0.1.0"
    contextual: ClassVar[tuple[str, ...]] = (
        "unix_path|port",
    )  # an instance is a door: it needs somewhere to listen
    setup_help = (
        "Make an instance per door (the + on a stage's page) and set a unix_path or a port. Give a door its own `source` and, if everything on it "
        "is one kind of thing, a `format` (ini, rfc822...): the format's formatter then splits what arrives. A door with neither path nor port does not listen."
    )
    config_example = '[plugins.provider.socket_ini]\nplugin = "socket"\nunix_path = "~/oaken-home/oaken-socket-ini"\nsource = "socket-ini"\nformat = "ini"\n'
    config_model = SocketConfig
    config: SocketConfig

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None  # the TCP port actually bound
        self.accepted = 0
        self.rejected = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._made_path: str | None = None

    async def start(self, ctx: Any) -> None:
        await super().start(ctx)
        cfg = self.config
        limit = cfg.max_item_bytes + 2
        try:
            if cfg.unix_path:
                path = os.path.expanduser(cfg.unix_path)
                await self._clear_stale(path)
                old = os.umask(0o177)  # the socket is born 0600, never briefly wider
                try:
                    self.server = await asyncio.start_unix_server(
                        self._handle, path, limit=limit
                    )
                finally:
                    os.umask(old)
                self._made_path = path
            else:
                self.server = await asyncio.start_server(
                    self._handle, LOOPBACK, cfg.port, limit=limit
                )
                self.port = self.server.sockets[0].getsockname()[1]
        except OSError as exc:
            raise PluginError(
                f"{self.ctx.name}: cannot listen on {self._wanted()}: {exc.strerror or exc}{self._hint(exc)}"
            ) from None

    def _wanted(self) -> str:
        c = self.config
        return (
            os.path.expanduser(c.unix_path) if c.unix_path else f"{LOOPBACK}:{c.port}"
        )

    def _hint(self, exc: OSError) -> str:
        if exc.errno == errno.EACCES:
            if self.config.unix_path:
                return f" (the daemon must be able to write to {os.path.dirname(self._wanted()) or '.'})"
            return (
                " (ports under 1024 need root: pick a higher one)"
                if (self.config.port or 0) < 1024
                else ""
            )
        return " (something else is using it)" if exc.errno == errno.EADDRINUSE else ""

    @staticmethod
    async def _clear_stale(path: str) -> None:
        """Remove a socket file nothing listens on; refuse one something does, and anything that is not a socket."""
        try:
            mode = os.stat(path).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise PluginError(f"{path} exists and is not a socket")
        try:
            _r, w = await asyncio.open_unix_connection(path)
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(path)
            return
        w.close()
        raise PluginError(f"{path} is already being listened on")

    async def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()
            for w in list(self._writers):
                w.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if self._made_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._made_path)
            self._made_path = None

    @property
    def where(self) -> str:
        return (
            os.path.expanduser(self.config.unix_path)
            if self.config.unix_path
            else f"{LOOPBACK}:{self.port}"
        )

    async def health(self) -> Health:
        if self.server is None or not self.server.is_serving():
            return Health(False, "not listening")
        return Health(
            True,
            f"listening on {self.where}: {self.accepted} accepted, {self.rejected} rejected",
        )

    # -- one connection ----------------------------------------------------------------------
    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            if self.config.framing == "lines":
                await self._lines(reader, writer)
            else:
                await self._whole(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the sender went away
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    async def _lines(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        pending = ""  # JSON read so far that is not yet a whole value (a pretty-printed document spans lines)
        while True:
            try:
                line = await reader.readuntil(b"\n")
                last = False
            except asyncio.IncompleteReadError as end:  # the last line had no newline
                line, last = end.partial, True
            except asyncio.LimitOverrunError:
                await self._reply(writer, "ERR item too large", ok=False)
                return
            if self.config.parse_json:
                pending = await self._json_lines(line, pending, writer)
                if pending is None:
                    return
                if last:
                    if pending.strip():
                        await self._reply(writer, "ERR not JSON", ok=False)
                    return
            elif line:
                await self._item(line, writer)
            if last:
                return

    async def _json_lines(
        self, line: bytes, pending: str, writer: asyncio.StreamWriter
    ) -> str | None:
        """Add a line to what is pending and submit every whole JSON value it completes. Returns what is still
        pending, or None when the connection must close. A value that is cut off at the end of the text is
        unfinished (more may follow); one that is wrong before its end is not JSON, and is dropped."""
        try:
            pending += line.decode("utf-8")
        except UnicodeDecodeError:
            await self._reply(writer, "ERR not UTF-8", ok=False)
            return ""
        while pending.strip():
            text = pending.lstrip()
            try:
                value, end = _DECODER.raw_decode(text)
            except json.JSONDecodeError as exc:
                if exc.pos >= len(text.rstrip()):  # ran out of text: wait for the rest
                    break
                await self._reply(writer, "ERR not JSON", ok=False)
                return ""
            pending = text[end:]
            for one in value if isinstance(value, list) else [value]:
                await self._submit(one, writer)
        else:
            pending = ""
        if len(pending) > self.config.max_item_bytes:
            await self._reply(writer, "ERR item too large", ok=False)
            return None
        return pending

    async def _whole(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        data = b""
        while chunk := await reader.read(65536):
            data += chunk
            if len(data) > self.config.max_item_bytes:
                await self._reply(writer, "ERR item too large", ok=False)
                return
        await self._item(data, writer, blank_ok=False)

    async def _item(
        self, raw: bytes, writer: asyncio.StreamWriter, *, blank_ok: bool = True
    ) -> None:
        text = raw.rstrip(b"\r\n") if self.config.framing == "lines" else raw
        if blank_ok and not text.strip():
            return  # an empty line is nothing, not an item
        if len(text) > self.config.max_item_bytes:
            return await self._reply(writer, "ERR item too large", ok=False)
        try:
            body: Any = text.decode("utf-8")
        except UnicodeDecodeError:
            return await self._reply(writer, "ERR not UTF-8", ok=False)
        bodies = [body]
        if self.config.parse_json:
            found = _records(body, whole=self.config.framing == "connection")
            if found is None:
                return await self._reply(writer, "ERR not JSON", ok=False)
            bodies = found
        for one in bodies:
            await self._submit(one, writer)

    async def _submit(self, body: Any, writer: asyncio.StreamWriter) -> None:
        try:
            subject = await self.submit(
                body,
                source=self.config.source or None,
                format=self.config.format or None,
                labels=self.config.labels or None,
            )
        except Exception as exc:  # noqa: BLE001 - the sender hears the kind of failure, never the detail
            self.ctx.log.warning("socket item not submitted", error=type(exc).__name__)
            return await self._reply(writer, f"ERR {type(exc).__name__}", ok=False)
        await self._reply(writer, f"OK {subject}", ok=True)

    async def _reply(
        self, writer: asyncio.StreamWriter, line: str, *, ok: bool
    ) -> None:
        self.accepted += ok
        self.rejected += not ok
        writer.write(line.encode() + b"\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
