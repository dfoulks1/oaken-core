# SPDX-License-Identifier: Apache-2.0
"""``start`` and ``restart`` run in the background unless ``--fg``. The processes are faked."""

from typing import Any

import pytest

from oaken_core.daemon import lifecycle


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(lifecycle, "detach", lambda **_: seen.append("detach") or {"pid": 1, "port": 2})
    monkeypatch.setattr(lifecycle, "run", lambda **_: seen.append("run") or 0)
    monkeypatch.setattr(lifecycle, "stop_gracefully", lambda **kw: seen.append(f"stop {kw}") or lifecycle.StopResult(True))
    return seen


def test_the_background_is_the_default_and_fg_stays_attached(calls: list[str]) -> None:
    assert lifecycle.cli(["start"]) == 0
    assert lifecycle.cli(["start", "--fg"]) == 0
    assert calls == ["detach", "run"]


def test_restart_stops_first_and_takes_a_grace(calls: list[str]) -> None:
    assert lifecycle.cli(["restart", "--grace", "3", "--no-kill", "--fg"]) == 0
    assert calls == ["stop {'grace': 3.0, 'kill': False}", "run"]


def test_a_running_core_is_reported_not_a_traceback(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from oaken_core.errors import Conflict

    def boom(**_: Any) -> None:
        raise Conflict("core is already running")

    monkeypatch.setattr(lifecycle, "detach", boom)
    assert lifecycle.cli(["start"]) == 1
    assert "already running" in capsys.readouterr().err
