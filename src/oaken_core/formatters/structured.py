# SPDX-License-Identifier: Apache-2.0
"""``json``: structured text split along its own seams, one part per top-level key."""

import json
from typing import Any, ClassVar

from oaken_core.parts import BODY, Part
from oaken_core.plugins.base import BaseFormatter


def _text(raw: Any) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


class JsonFormatter(BaseFormatter):
    """A JSON object becomes one part per top-level key (the value is the part's content). Anything else
    (a list, a number) stays one ``body`` part. Text that is not JSON raises, and intake keeps the item whole."""

    api_version = 1  # the plugin API this was written against (core's PLUGIN_API_VERSION)
    requires_core = ">=0.0.0"  # the oaken-core versions it works with
    version: ClassVar[str] = "1"
    formats: ClassVar[tuple[str, ...]] = ("json", "application/json")

    async def split(self, raw: Any) -> dict[str, Part]:
        data = raw if isinstance(raw, dict | list) else json.loads(_text(raw))
        if not isinstance(data, dict) or not data:
            return {BODY: Part(name=BODY, content_type="application/json", content=data)}
        return {str(k): Part(name=str(k), content_type="application/json", content=v) for k, v in data.items()}
