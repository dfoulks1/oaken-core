# SPDX-License-Identifier: Apache-2.0
"""The formatters core ships: they split a raw item into named parts before triage.

Each is deterministic and stdlib-only (no model, nothing stored). A product ships richer ones for its own
formats (an email parser, an eFax reader) as ordinary ``formatter`` plugins.
"""
