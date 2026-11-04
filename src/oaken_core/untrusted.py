# SPDX-License-Identifier: Apache-2.0
"""Mail-derived text that must never be treated as an instruction.

``UntrustedText`` wraps any string that came from a message. There is deliberately no ``__str__``,
so the text cannot slip into an f-string or a log line by accident. The only way to put it in front
of a model is ``render``, which fences it in a delimiter block carrying a random, per-call nonce
and neutralizes anything inside the text that looks like such a delimiter.

The fence is a courtesy to the model, not a security boundary on its own: the boundary is that
mail text only ever travels in the user turn, as data, and that nothing a model says is executed.
See ``oaken.ai.prompts`` for the builder that enforces the first half.
"""

import re
import secrets
import unicodedata
from dataclasses import dataclass, field

# Characters that can pass for ``<`` or ``>`` once folded, beyond what NFKC already maps.
_LEFT = frozenset("<＜﹤‹〈⟨❮〈«≪ⰺ")
_RIGHT = frozenset(">＞﹥›〉⟩❯〉»≫ⰻ")
_RUN = re.compile(r"[<>]{2,}")
_LABEL = re.compile(r"[^A-Za-z0-9_.:-]+")
SAFE_LEFT, SAFE_RIGHT = "‹", "›"  # single angle quotes: visible, and not delimiters


def _fold(ch: str) -> str:
    """One character standing for ``ch``: ``<``, ``>`` or ``ch`` itself. Never changes length."""
    if ch in _LEFT:
        return "<"
    if ch in _RIGHT:
        return ">"
    norm = unicodedata.normalize("NFKC", ch)
    if len(norm) == 1 and norm in "<>":
        return norm
    return ch


def neutralize(text: str) -> str:
    """Remove invisible format characters (zero-width, bidi controls), then replace every run of
    two or more angle brackets or look-alikes with harmless single angle quotes, so no delimiter
    can be forged or hidden inside the text."""
    clean = "".join(c for c in text if unicodedata.category(c) != "Cf")
    folded = "".join(_fold(c) for c in clean)
    out: list[str] = []
    last = 0
    for run in _RUN.finditer(folded):
        out.append(clean[last : run.start()])
        out.append(
            "".join(SAFE_LEFT if folded[i] == "<" else SAFE_RIGHT for i in range(*run.span()))
        )
        last = run.end()
    out.append(clean[last:])
    return "".join(out)


@dataclass(frozen=True, slots=True)
class UntrustedText:
    value: str = field(repr=False)
    label: str = "email"  # what the block is called to the model (``subject``, ``body`` ...)

    def unwrap(self) -> str:
        """The raw text. Callers are responsible for keeping it out of system prompts."""
        return self.value

    def render(self, label: str | None = None, *, nonce: str | None = None) -> str:
        """The text inside a randomized delimiter block, e.g.::

            <<<EMAIL label=body nonce=3f9c0a51e2b7d4a8>>>
            ...text...
            <<<END nonce=3f9c0a51e2b7d4a8>>>

        A fresh nonce is drawn on every call, so nothing in the mail can predict it. ``nonce`` is
        for tests."""
        tag = nonce or secrets.token_hex(8)
        name = _LABEL.sub("_", label or self.label) or "email"
        return (
            f"<<<EMAIL label={name} nonce={tag}>>>\n{neutralize(self.value)}\n<<<END nonce={tag}>>>"
        )

    def __str__(self) -> str:
        raise TypeError("UntrustedText has no implicit str(); build prompts with the builder")

    def __repr__(self) -> str:
        return f"UntrustedText(<{len(self.value)} chars>)"

    def __len__(self) -> int:
        return len(self.value)
