# SPDX-License-Identifier: Apache-2.0
"""The runtime file: private, found by others, single-instance, stale files replaced."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from oaken_core.api.ratelimit import RateLimiter
from oaken_core.api.runtime import RuntimeFile, pid_alive, read_runtime
from oaken_core.clock import FakeClock
from oaken_core.errors import Conflict


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait()
    return proc.pid


def make(path: Path) -> RuntimeFile:
    return RuntimeFile(path, FakeClock(), "9.9.9")


def test_the_file_has_pid_host_port_version_and_start_time(tmp_path: Path) -> None:
    f = make(tmp_path / "runtime.json")
    f.claim()
    assert read_runtime(f.path) == {**(read_runtime(f.path) or {}), "port": 0}, "starting"
    f.publish(4242)
    data = read_runtime(f.path)
    assert data is not None
    assert data["pid"] == os.getpid() and data["host"] == "127.0.0.1" and data["port"] == 4242
    assert data["version"] == "9.9.9" and data["started_at"].endswith("Z")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_is_private_from_the_first_byte_and_after_publish(tmp_path: Path) -> None:
    f = make(tmp_path / "runtime.json")
    f.claim()
    assert f.path.stat().st_mode & 0o777 == 0o600
    f.publish(1)
    assert f.path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp")), "the temp file is renamed away"


def test_a_clean_release_removes_the_file(tmp_path: Path) -> None:
    f = make(tmp_path / "runtime.json")
    f.claim()
    f.release()
    assert not f.path.exists()


def test_a_second_instance_is_refused_while_the_first_lives(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"pid": os.getppid(), "port": 1}))  # a live process that is not us
    with pytest.raises(Conflict):
        make(path).claim()
    assert read_runtime(path) == {"pid": os.getppid(), "port": 1}, "not touched"


def test_a_stale_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"pid": dead_pid(), "port": 1}))
    f = make(path)
    f.claim()
    data = read_runtime(path)
    assert data is not None and data["pid"] == os.getpid()


def test_an_unreadable_file_is_replaced_too(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{half a file")
    make(path).claim()
    assert read_runtime(path) is not None


def test_release_never_removes_someone_elses_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    f = make(path)
    f.claim()
    path.write_text(json.dumps({"pid": os.getppid(), "port": 5}))
    f.release()
    assert path.exists()


def test_pid_alive() -> None:
    assert pid_alive(os.getpid()) and not pid_alive(dead_pid()) and not pid_alive(0)


def test_the_rate_limiter_is_a_token_bucket_per_client() -> None:
    now = [0.0]
    limiter = RateLimiter(60, now=lambda: now[0], burst=2)
    assert limiter.take("a") == 0 and limiter.take("a") == 0
    wait = limiter.take("a")
    assert 0 < wait <= 1.0 + 1e-9
    assert limiter.take("b") == 0, "another client has its own bucket"
    now[0] += 1.0
    assert limiter.take("a") == 0
