# SPDX-License-Identifier: Apache-2.0
"""The triage core ships so that intake works with nothing else installed: ``dedupe`` drops what a source has
already sent, ``rules`` applies the owner's own deterministic rules. Both are plugins like any other (switch off,
replace or configure them); core just carries them."""
