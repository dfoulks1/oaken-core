# SPDX-License-Identifier: Apache-2.0
"""Make untrusted text safe to print to a terminal.

Outside text is data, never instructions, and a terminal is an interpreter: a
crafted subject or body can move the cursor, rewrite earlier lines, relabel the window title,
smuggle clipboard writes, or use Unicode bidi overrides to make text read the opposite of what it
says. Any outside field that reaches a TTY goes through :func:`sanitize` first.

What is neutralized
-------------------
* **C0 control characters** (``U+0000``–``U+001F``) except ``\\t`` and ``\\n``, and **DEL**
  (``U+007F``) — this removes the ESC that begins every ANSI/CSI/OSC sequence, so those sequences
  become inert text.
* **C1 control characters** (``U+0080``–``U+009F``), including the 8-bit CSI/OSC introducers.
* **Bidi and invisible formatting** code points that can reorder or hide text:
  the explicit bidi overrides/embeddings/isolates (LRE/RLE/PDF/LRO/RLO/LRI/RLI/FSI/PDI),
  the zero-width joiners/non-joiners and zero-width space, and the BOM/zero-width no-break space.

Each removed code point is replaced with U+FFFD (``\\ufffd``), so the output keeps the *shape* of
the original — a reviewer sees that something was stripped — without emitting anything the terminal
will act on. ``\\t`` and ``\\n`` are preserved because they are ordinary layout, not control.

This is for **terminal output only**. ``--json`` output is the original data, unchanged: a script
consuming JSON is not a terminal and must receive the bytes as they are .
"""

from __future__ import annotations

# Bidi/zero-width formatting code points that reorder or hide text.
_FORMAT_CODEPOINTS = frozenset(
    {
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x200D,  # zero-width joiner
        0x200E,  # left-to-right mark
        0x200F,  # right-to-left mark
        0x202A,  # left-to-right embedding
        0x202B,  # right-to-left embedding
        0x202C,  # pop directional formatting
        0x202D,  # left-to-right override
        0x202E,  # right-to-left override
        0x2066,  # left-to-right isolate
        0x2067,  # right-to-left isolate
        0x2068,  # first strong isolate
        0x2069,  # pop directional isolate
        0xFEFF,  # BOM / zero-width no-break space
    }
)

_KEEP_C0 = frozenset({0x09, 0x0A})  # tab, newline
_REPLACEMENT = "\ufffd"


def _unsafe(cp: int) -> bool:
    if cp in _KEEP_C0:
        return False
    if cp < 0x20 or cp == 0x7F:  # C0 controls + DEL (includes ESC 0x1B)
        return True
    if 0x80 <= cp <= 0x9F:  # C1 controls (8-bit CSI/OSC introducers)
        return True
    return cp in _FORMAT_CODEPOINTS


def sanitize(text: str) -> str:
    """Return ``text`` with every control/escape/bidi code point replaced by U+FFFD.

    Safe to call on any string; plain text passes through unchanged. ``\\t`` and ``\\n`` survive.
    """
    if not any(_unsafe(ord(ch)) for ch in text):
        return text
    return "".join(_REPLACEMENT if _unsafe(ord(ch)) else ch for ch in text)


def sanitize_line(text: str) -> str:
    """Like :func:`sanitize`, but also folds newlines/tabs to single spaces — for one-line fields
    such as a subject shown in a table row, where a stray newline would break the layout."""
    return " ".join(sanitize(text).split())
