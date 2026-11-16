# SPDX-License-Identifier: Apache-2.0
"""The confidentiality helpers, and an agent's locality: stated by the person, strictest by default."""

import pytest

from oaken_core.agents import AgentRegistry
from oaken_core.confidential import is_confidential, may_see_confidential
from oaken_core.errors import ConfigError
from oaken_core.settings import SwarmSettings

from .conftest import Kit


def test_only_a_true_label_is_confidential() -> None:
    assert is_confidential({"confidential": "true"})
    assert not is_confidential({"confidential": "false"})
    assert not is_confidential({})


def test_only_local_may_see_confidential() -> None:
    assert may_see_confidential("local")
    assert not may_see_confidential("lan")
    assert not may_see_confidential("hosted")
    assert not may_see_confidential("")  # unknown is never local


@pytest.fixture
def agents(kit: Kit) -> AgentRegistry:
    return AgentRegistry(kit.db, kit.clock, SwarmSettings(enabled=True, allowed_kinds=["llm.summarize"]))


async def test_an_agent_is_hosted_unless_the_person_says_local(kit: Kit, agents: AgentRegistry) -> None:
    hosted, _ = await agents.create("cloud", ["llm.summarize"])
    assert hosted.locality == "hosted" and not hosted.may_see_confidential
    local, token = await agents.create("desk", ["llm.summarize"], locality="local")
    assert local.may_see_confidential
    assert (await agents.authenticate(token)).locality == "local"  # stored, not just echoed
    with pytest.raises(ConfigError, match="locality"):
        await agents.create("odd", ["llm.summarize"], locality="moon")


async def test_an_unknown_stored_locality_reads_as_hosted(kit: Kit, agents: AgentRegistry) -> None:
    agent, _ = await agents.create("desk", ["llm.summarize"], locality="local")
    with pytest.raises(Exception, match="CHECK"):  # the schema refuses it outright
        await kit.db.execute("UPDATE agents SET locality = 'moon' WHERE id = ?", (agent.id,))
    assert (await agents.get(agent.id)).locality == "local"
