# SPDX-License-Identifier: Apache-2.0
"""The shipped formatter splits along its format's own seams; the rest is intake's fallback."""

import pytest

from oaken_core.formatters.structured import JsonFormatter


async def test_json_splits_an_object_by_key_and_keeps_anything_else_whole() -> None:
    parts = await JsonFormatter().split('{"title": "Hi", "tags": ["a"]}')
    assert {n: p.content for n, p in parts.items()} == {"title": "Hi", "tags": ["a"]}
    assert list(await JsonFormatter().split("[1, 2]")) == ["body"]
    with pytest.raises(ValueError):
        await JsonFormatter().split("not json")  # intake keeps such an item whole
