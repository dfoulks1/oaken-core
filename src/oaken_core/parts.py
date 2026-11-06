# SPDX-License-Identifier: Apache-2.0
"""A part: one named piece of an item (an email's headers, its body, an attachment; a fax's metadata).

Core has no nouns: it carries parts, it does not say what they mean. A formatter splits an item into
parts, an ingestor stores them, and an analyzer asks for the ones it ``reads``. Names are free strings
that those three agree on, the way a label key is agreed.

shortcut: ``content`` rides in the job payload as JSON, so it must be text or JSON; upgrade to a
by-reference form (a path or hash) when attachments or images have to travel.
"""

from typing import Any

from pydantic import BaseModel, Field

BODY = "body"  # what an item with no format is: one part


class Part(BaseModel):
    name: str
    content_type: str = "text/plain"
    content: Any = None
    labels: dict[str, str] = Field(default_factory=dict)  # set on the part's own subject once stored


def whole(body: Any) -> dict[str, Part]:
    """An item with no formatter: the raw body as its one part."""
    return {BODY: Part(name=BODY, content=body)}
