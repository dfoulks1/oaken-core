# SPDX-License-Identifier: Apache-2.0
import asyncio
import os
import stat
from pathlib import Path

import pytest
from oaken_core.clock import FakeClock
from oaken_core.errors import PluginError
from oaken_core.logging import get_logger
from oaken_core.plugins.contracts import PluginContext
from oaken_core.providers.socket import SocketProvider


class Intake:
    def __init__(self) -> None:
        self.items: list[tuple[str, object]] = []
        self.broken = False
        self.labels: dict[str, str] = {}

    async def submit(
        self,
        source: str,
        body: object,
        *,
        labels: dict[str, str],
        key: str | None = None,
        format: str | None = None,
    ) -> str:
        if self.broken:
            raise RuntimeError("secret detail")
        self.items.append((source, body))
        self.labels = labels
        return f"s{len(self.items)}"


@pytest.fixture
def intake() -> Intake:
    return Intake()


async def start(intake: Intake, **config: object) -> SocketProvider:
    p = SocketProvider()
    await p.start(PluginContext(name="inbox", kind="provider", clock=FakeClock(), log=get_logger("t"), config=dict(config), submit=intake.submit))  # type: ignore[arg-type]
    return p


async def talk(
    p: SocketProvider, send: bytes, *, eof: bool = False, replies: int = 1
) -> list[str]:
    if p.config.unix_path:
        r, w = await asyncio.open_unix_connection(p.config.unix_path)
    else:
        r, w = await asyncio.open_connection("127.0.0.1", p.port)
    w.write(send)
    if eof:
        w.write_eof()
    out = [
        (await asyncio.wait_for(r.readline(), 2)).decode().strip()
        for _ in range(replies)
    ]
    w.close()
    return out


async def test_each_line_sent_to_a_unix_socket_is_submitted_and_answered(
    tmp_path: Path, intake: Intake
) -> None:
    p = await start(intake, unix_path=str(tmp_path / "s"))
    try:
        assert await talk(
            p, b'{"n": 1}\n{"n": 2}\n\n{"n": 3}', eof=True, replies=3
        ) == [
            "OK s1",
            "OK s2",
            "OK s3",
        ]  # JSON lines: a blank line is nothing; a last line with no newline counts once the sender closes
        assert intake.items == [
            ("inbox", {"n": 1}),
            ("inbox", {"n": 2}),
            ("inbox", {"n": 3}),
        ]
        assert stat.S_IMODE(os.stat(tmp_path / "s").st_mode) == 0o600
        assert (await p.health()).ok and "3 accepted" in (await p.health()).detail
    finally:
        await p.stop()
    assert not (tmp_path / "s").exists()  # a stopped listener leaves no file behind


async def test_tcp_listens_on_loopback_and_reports_its_port(intake: Intake) -> None:
    p = await start(intake, port=0, source="faxes", parse_json=False)
    try:
        assert (
            p.port
            and p.server is not None
            and p.server.sockets[0].getsockname()[0] == "127.0.0.1"
        )
        assert await talk(p, b"hello\n") == ["OK s1"] and intake.items == [
            ("faxes", "hello")
        ]
    finally:
        await p.stop()


async def test_connection_framing_makes_one_item_of_everything_up_to_the_close(
    intake: Intake,
) -> None:
    p = await start(intake, port=0, framing="connection", parse_json=False)
    try:
        assert await talk(p, b"line one\nline two\n", eof=True) == ["OK s1"]
        assert intake.items == [("inbox", "line one\nline two\n")]
    finally:
        await p.stop()


async def test_json_is_the_default_and_what_is_not_json_is_refused_without_closing(
    intake: Intake,
) -> None:
    p = await start(intake, port=0)
    try:
        assert await talk(p, b'{"a": 1}\nnope\n', replies=2) == [
            "OK s1",
            "ERR not JSON",
        ]
        assert intake.items == [("inbox", {"a": 1})]
        assert (await p.health()).detail.endswith("1 accepted, 1 rejected")
    finally:
        await p.stop()


async def test_too_large_and_not_utf8_and_a_failing_intake_get_an_error_and_no_detail(
    intake: Intake,
) -> None:
    p = await start(intake, port=0, max_item_bytes=8)
    try:
        assert await talk(p, b"x" * 100 + b"\n") == ["ERR item too large"]
        assert await talk(p, b"\xff\xfe\n") == ["ERR not UTF-8"]
        intake.broken = True
        reply = await talk(p, b'{"a": 1}\n')
        assert reply == ["ERR RuntimeError"] and "secret" not in reply[0]
    finally:
        await p.stop()


async def test_it_refuses_a_busy_socket_replaces_a_stale_one_and_needs_exactly_one_place(
    tmp_path: Path, intake: Intake
) -> None:
    path = str(tmp_path / "s")
    first = await start(intake, unix_path=path)
    try:
        with pytest.raises(PluginError, match="already being listened on"):
            await start(intake, unix_path=path)
    finally:
        await first.stop()
    Path(path).write_text("not a socket")
    with pytest.raises(PluginError, match="not a socket"):
        await start(intake, unix_path=path)
    Path(path).unlink()
    stale, _ = (
        await asyncio.start_unix_server(lambda r, w: None, path),
        None,
    )  # a socket file with nobody behind it
    stale.close()
    await stale.wait_closed()
    again = await start(intake, unix_path=path)  # the dead file is replaced
    await again.stop()
    for bad in ({}, {"unix_path": path, "port": 5}):
        with pytest.raises(PluginError, match="set exactly one"):
            await start(intake, **bad)


async def test_an_array_is_one_item_per_element_and_a_connection_may_be_a_document_or_json_lines(
    intake: Intake,
) -> None:
    p = await start(intake, port=0)
    try:
        assert await talk(p, b'[{"n": 1}, {"n": 2}]\n', replies=2) == ["OK s1", "OK s2"]
        assert intake.items == [("inbox", {"n": 1}), ("inbox", {"n": 2})]
    finally:
        await p.stop()
    p = await start(intake, port=0, framing="connection")
    try:
        assert await talk(p, b'{\n  "n": 3\n}\n', eof=True) == [
            "OK s3"
        ]  # one pretty-printed document
        assert await talk(p, b'{"n": 4}\n{"n": 5}\n', eof=True, replies=2) == [
            "OK s4",
            "OK s5",
        ]  # JSON Lines
        assert await talk(p, b'{"n": 6}\nnope\n', eof=True) == [
            "ERR not JSON"
        ]  # all or nothing
        assert [b for _, b in intake.items] == [
            {"n": 1},
            {"n": 2},
            {"n": 3},
            {"n": 4},
            {"n": 5},
        ]
    finally:
        await p.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
async def test_a_socket_in_a_directory_it_cannot_write_says_where_and_why(
    tmp_path: Path, intake: Intake
) -> None:
    (tmp_path / "ro").mkdir(mode=0o500)
    with pytest.raises(
        PluginError,
        match=rf"cannot listen on {tmp_path}/ro/s: Permission denied \(the daemon must be able to write to {tmp_path}/ro\)",
    ):
        await start(intake, unix_path=str(tmp_path / "ro" / "s"))


async def test_a_port_already_in_use_says_so(intake: Intake) -> None:
    first = await start(intake, port=0)
    try:
        with pytest.raises(
            PluginError,
            match=r"cannot listen on 127.0.0.1:\d+: .*\(something else is using it\)",
        ):
            await start(intake, port=first.port)
    finally:
        await first.stop()


async def test_lines_framing_takes_a_pretty_printed_document_an_array_and_several_values_on_one_line(
    intake: Intake,
) -> None:
    p = await start(intake, port=0)
    try:
        doc = b'[\n {\n  "n": 1,\n  "s": "a b"\n },\n {"n": 2}\n]\n'  # what json.dumps(indent=1) prints
        assert await talk(p, doc, eof=True, replies=2) == ["OK s1", "OK s2"]
        assert await talk(p, b'{"n": 3} {"n": 4}\n{"n": 5}\n', eof=True, replies=3) == [
            "OK s3",
            "OK s4",
            "OK s5",
        ]
        assert [b for _, b in intake.items] == [
            {"n": 1, "s": "a b"},
            {"n": 2},
            {"n": 3},
            {"n": 4},
            {"n": 5},
        ]
    finally:
        await p.stop()


async def test_garbage_is_refused_without_stopping_later_values_and_a_cut_off_value_is_refused_at_the_end(
    intake: Intake,
) -> None:
    p = await start(intake, port=0)
    try:
        assert await talk(p, b'{"n": 1}\nnope\n{"n": 2}\n', eof=True, replies=3) == [
            "OK s1",
            "ERR not JSON",
            "OK s2",
        ]
        assert await talk(p, b'{"n": 3,\n', eof=True) == [
            "ERR not JSON"
        ]  # the sender closed mid-value
    finally:
        await p.stop()


async def test_several_sockets_run_at_once_each_with_its_own_path_source_and_format(
    tmp_path: Path,
) -> None:
    class Seen(Intake):
        def __init__(self) -> None:
            super().__init__()
            self.formats: list[tuple[str, str | None]] = []

        async def submit(
            self,
            source: str,
            body: object,
            *,
            labels: dict[str, str],
            key: str | None = None,
            format: str | None = None,
        ) -> str:
            self.formats.append((source, format))
            return await super().submit(
                source, body, labels=labels, key=key, format=format
            )

    seen = Seen()
    door = await start(seen, unix_path=str(tmp_path / "a"), source="socket-a")
    typed = await start(
        seen, unix_path=str(tmp_path / "b"), source="socket-ini", format="ini"
    )  # a typed door beside the general one
    try:
        assert (await talk(door, b'{"x": 1}\n'))[0].startswith("OK")
        assert (await talk(typed, b'{"x": 2}\n'))[0].startswith("OK")
    finally:
        await door.stop()
        await typed.stop()
    assert seen.formats == [("socket-a", None), ("socket-ini", "ini")]


def test_a_socket_is_a_template_that_needs_a_path_or_a_port_and_a_port_of_zero_counts() -> None:
    from oaken_core.plugins.base import BaseTemplatePlugin, missing_contextual

    need = SocketProvider.contextual
    assert issubclass(SocketProvider, BaseTemplatePlugin)
    assert missing_contextual(need, {}) == [
        "unix_path or port"
    ]  # bare: a template, waiting for an instance
    assert (
        missing_contextual(need, {"unix_path": "/tmp/s"}) == []
        and missing_contextual(need, {"port": 7777}) == []
    )
    assert (
        missing_contextual(need, {"port": 0}) == []
    )  # 0 asks for any free port: that is a door


async def test_labels_in_config_ride_on_every_item(
    tmp_path: Path, intake: Intake
) -> None:
    p = await start(
        intake, unix_path=str(tmp_path / "s"), labels={"confidential": "true"}
    )
    try:
        assert await talk(p, b'{"n": 1}\n') == ["OK s1"]
        assert intake.labels == {"confidential": "true"}
    finally:
        await p.stop()
