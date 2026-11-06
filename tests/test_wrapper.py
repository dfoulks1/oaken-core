# SPDX-License-Identifier: Apache-2.0
"""A product built on core: its own name, home folder and command, over the same library."""

import contextlib
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from oaken_core import identity, paths, run
from oaken_core.daemon.core import Core
from oaken_core.operations.openapi import build_openapi
from oaken_core.settings import Settings
from oaken_core.store import SqliteStore

from .test_host import EP


@pytest.fixture(autouse=True)
def default_identity() -> Iterator[None]:
    yield
    identity.configure()  # a test that wraps core must not rename the next one


def test_a_product_has_its_own_home_variable_and_never_borrows_oakens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path / "mail"))  # another Oaken's home, set in the shell
    monkeypatch.setenv("OAKEN_GROVE_HOME", str(tmp_path / "grove"))
    assert paths.config_dir() == tmp_path / "mail" / "config"  # plain core
    identity.configure(app="oaken-grove", title="Oaken CRM", prog="grove")
    assert identity.home_env() == "OAKEN_GROVE_HOME"
    assert paths.config_dir() == tmp_path / "grove" / "config"
    monkeypatch.delenv("OAKEN_GROVE_HOME")
    assert "mail" not in str(paths.config_dir())  # no fallback to OAKEN_HOME


def test_the_command_runs_under_the_products_name(capsys: pytest.CaptureFixture[str]) -> None:
    with contextlib.suppress(SystemExit):  # argparse exits after --help, or the CLI returns its code
        run(["--help"], app="oaken-grove", title="Oaken CRM", prog="grove")
    assert "usage: grove" in capsys.readouterr().out


async def test_the_console_and_openapi_carry_the_products_title(tmp_path: Path) -> None:
    identity.configure(app="oaken-grove", title="Oaken CRM", prog="grove")
    tables = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}}
    core = Core(Settings.model_validate({"console": {"port": 0}, "plugins": {"tables": tables}}),
                discover=lambda g: {"oaken_core.stores": [EP("sqlite", SqliteStore)]}.get(g, []))
    await core.open()
    core.console.bind(8321)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=core.console), base_url="http://127.0.0.1:8321")
    try:
        page = (await client.get("/")).text
        assert "Oaken CRM" in page and "Oaken Core" not in page
        assert '<span class="wordmark">Oaken<span>CRM</span>' in page
        assert (await client.get("/config")).status_code == 200
    finally:
        await client.aclose()
        await core.stop()
    assert build_openapi([])["info"]["title"] == "Oaken CRM"


async def test_a_products_plugins_load_through_entry_points_from_an_installed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real discovery, no stubs: a package on sys.path with an entry_points.txt, like a wheel."""
    import importlib

    site = tmp_path / "site"
    (site / "grove_plugins-0.1.dist-info").mkdir(parents=True)
    (site / "grove_plugins-0.1.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: grove-plugins\nVersion: 0.1\n")
    (site / "grove_plugins-0.1.dist-info" / "entry_points.txt").write_text("[oaken_core.triagers]\ncrm = grove_plugins:CrmTriager\n")
    (site / "grove_plugins.py").write_text(
        "from oaken_core.plugins.base import BasePlugin\n\n"
        "class CrmTriager(BasePlugin):\n"
        "    version = '1'\n    needs_llm = False\n    rank = 50\n"
        "    async def triage(self, item):\n        return None\n"
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    identity.configure(app="oaken-grove", title="Oaken CRM", prog="grove")
    tables = {"store": {"sqlite": {"path": str(tmp_path / "core.db")}}, "triager": {"crm": {}}}
    queues = {"triage": {"kind": "triager", "label": "Triage"}}
    core = Core(Settings.model_validate({"console": {"port": 0}, "queues": queues, "plugins": {"tables": tables}}))  # default discovery
    await core.open()
    await core.host.start()
    try:
        crm = next(s for s in core.host.statuses() if (s.kind, s.name) == ("triager", "crm"))
        assert crm.started and crm.healthy, crm
        assert [q.name for q in core.client.svc.queues.all() if not q.hidden] == ["default", "triage"]
    finally:
        await core.host.stop()
        await core.stop()
