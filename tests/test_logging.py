# SPDX-License-Identifier: Apache-2.0
import io
import json
import logging
import stat
from pathlib import Path

import pytest

from oaken_core.logging import configure_logging, get_logger, log_context
from oaken_core.settings import LogSettings


@pytest.fixture(autouse=True)
def _reset() -> None:
    yield
    configure_logging(LogSettings(), io.StringIO())
    logging.getLogger("oaken_core").handlers.clear()


def run(settings: LogSettings, emit) -> list[dict]:  # type: ignore[no-untyped-def]
    buf = io.StringIO()
    configure_logging(settings, buf)
    emit()
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_all_five_levels_exist_and_filter() -> None:
    log = get_logger("oaken_core.t")

    def emit() -> None:
        log.debug("d")
        log.info("i")
        log.warning("w")
        log.error("e")
        log.critical("c")

    for level, expect in [
        ("DEBUG", "diwec"), ("INFO", "iwec"), ("WARNING", "wec"), ("ERROR", "ec"), ("CRITICAL", "c"),
    ]:  # fmt: skip
        lines = run(LogSettings(level=level, format="json"), emit)
        assert "".join(e["event"] for e in lines) == expect


def test_levels_are_case_insensitive() -> None:
    assert LogSettings(level="warning", levels={"oaken_core.x": "debug"}).levels == {"oaken_core.x": "DEBUG"}


def test_a_per_logger_level_turns_one_module_up_and_reset_turns_it_off() -> None:
    quiet, loud = get_logger("oaken_core.quiet"), get_logger("oaken_core.loud")

    def emit() -> None:
        quiet.debug("q")
        loud.debug("l")

    lines = run(LogSettings(level="WARNING", levels={"oaken_core.loud": "DEBUG"}, format="json"), emit)
    assert [e["event"] for e in lines] == ["l"]
    assert run(LogSettings(level="WARNING", format="json"), emit) == []  # the override is gone


def test_fields_come_through_secrets_are_redacted_and_long_values_cut() -> None:
    log = get_logger("oaken_core.t")
    (e,) = run(
        LogSettings(format="json"),
        lambda: log.info("hi", job_id="j1", api_token="s3cret", body="x" * 500),
    )
    assert e["job_id"] == "j1" and e["api_token"] == "[REDACTED]"
    assert e["body"].endswith("[truncated 300 chars]")


def test_context_fields_are_added_inside_the_block_only() -> None:
    log = get_logger("oaken_core.t")

    def emit() -> None:
        with log_context(job_id="j9"):
            log.info("inside")
        log.info("outside")

    inside, outside = run(LogSettings(format="json"), emit)
    assert inside["job_id"] == "j9" and "job_id" not in outside


def test_an_exception_logs_its_type_and_frames_never_its_message() -> None:
    log = get_logger("oaken_core.t")

    def emit() -> None:
        try:
            raise ValueError("private text")
        except ValueError:
            log.error("boom", exc_info=True)

    (e,) = run(LogSettings(format="json"), emit)
    assert e["exception"] == "ValueError" and e["frames"]
    assert "private text" not in json.dumps(e)


def test_console_format_is_one_readable_line() -> None:
    buf = io.StringIO()
    configure_logging(LogSettings(format="console"), buf)
    get_logger("oaken_core.t").warning("careful", job_id="j1")
    line = buf.getvalue().strip()
    assert "WARNING" in line and "careful" in line and "job_id=j1" in line


def test_a_file_is_json_owner_only_and_the_host_root_logger_is_untouched(tmp_path: Path) -> None:
    root_handlers = list(logging.getLogger().handlers)
    target = tmp_path / "log" / "core.log"
    configure_logging(LogSettings(file=str(target), format="console"), io.StringIO())
    get_logger("oaken_core.t").info("to file", n=1)
    for h in logging.getLogger("oaken_core").handlers:
        h.flush()
    assert json.loads(target.read_text().splitlines()[0])["n"] == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert logging.getLogger().handlers == root_handlers
