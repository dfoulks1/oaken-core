# SPDX-License-Identifier: Apache-2.0
"""``nearjunk``: rejects an intake item that sits close to junk the owner has already seen, with no
model call. Rank 40, between the rules (10) and a model (90). Does nothing while ``[router]
junk_index`` is off."""

from typing import ClassVar

from oaken_core.errors import Unavailable
from oaken_core.intake import IntakeItem, Verdict, item_text
from oaken_core.plugins.base import BaseTriager, BaseVecAnalyzer


class NearJunk(BaseVecAnalyzer, BaseTriager):
    rank: ClassVar[int] = 40
    version: ClassVar[str] = "1"

    async def triage(self, item: IntakeItem) -> Verdict | None:
        router = self.ctx.router
        if router is None:
            raise Unavailable("nearjunk has no router")
        hit = await router.near_junk(item_text(item), confidential=item.labels.get("confidential") == "true")
        return Verdict("reject", f"near junk ({hit.score:.2f})") if hit else None
