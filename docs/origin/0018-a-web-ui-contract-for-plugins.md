# 18. A web UI contract for plugins: settings, pages and wizards from data

- Status: accepted (ratified 2026-11-03 by the owner, who verified the built implementation for 0.2.0)
- Date: 2026-11-03
- Rests on: ADR 0017 (built first); ADR 0016 is built on this one
- Amends: ADR 0012 (a plugin gains one optional method, `ui()`, and one context-free declaration) and
  ADR 0013 (a plugin's pages are shown by core; the no-script rule stands)
- Built: 2026-11-03, not yet verified by the owner's checks: `UiSpec` and its parts, `ui()`, the
  check and fallback (`plugins/ui.py`, `PluginHost.plugin_ui`), position-field ordering with
  downshift (`reposition`), the console's `/plugins/<kind>/<name>/<page>`, and the page and button
  actions. Not built: settings sections and `show_if` in the modal, and the CLI walking a page.

## Context

The owner: "a webui contract that would allow you to extend the webui without writing a bunch of
stuff to oaken-core? yes please." Scope: "customizing settings modal, or adding a new wizard for
your plugin", and "you should be able to add an endpoint and serve a form or something locally, and
customize what the modal offers." The aim is that the plugin that extends everything else can extend
the console too.

Premises, each checked on 2026-11-03:

- The settings modal already renders from a plugin's pydantic config schema: the type picks the
  input and `description` is the hint (`_plugins.html`). It has no sections, labels, order or
  conditional fields, and `model`, `rules`, `skip_rules` and `steps` are special-cased in the template.
- The CLI already builds flags from an operation's input schema (`schema_flags.py`).
- ADR 0013 and CORE_50: the console has no script, a strict CSP and CSRF-guarded forms; it shows
  counts and health, never mail text.
- Plugin operations are namespaced `<plugin>.<name>`, checked and audited by the registry, and a
  `mutate` is confirmed (ADR 0012, `plugin_ops.py`).

## Decision

1. **A plugin declares its UI as data, never markup.** `BasePlugin` gains an optional `ui()` returning
   a `UiSpec`: plain frozen dataclasses for **sections** (a title and the config fields in it),
   **buttons** (a label and one of the plugin's own operations) and **pages**. A field's own hints
   ride on the config model, as `description` does now: `Field(json_schema_extra={"ui": {"label",
   "section", "order", "show_if", "help"}})`.
2. **A page is an endpoint core serves.** A page has a name, a title and fields of the schema's types
   (string, number, boolean, enum, list of strings), a `load` read operation that returns the values and
   messages, an optional `test` read operation (a Test button), and a `submit` operation with confirm
   text. Core serves it at `/plugins/<name>/<page>` and renders it. A plugin sends no HTML, CSS, script
   or link: core owns the chrome, escaping, length caps, CSRF and the CSP.
3. **Actions run the plugin's own operations, only.** Core routes the button or submit to
   `<plugin>.<operation>`, checks the plugin owns it, and keeps its permission: a `mutate` is confirmed.
   A plugin's UI cannot call core's or another plugin's operations.
4. **A wizard is a page.** One page: all fields, Test, Confirm. The CLI walks the same declaration one
   field at a time when stdin is a terminal and required fields are missing; with `--json`, flags or no
   terminal, nothing changes.
4a. **An orderable list is a position field, not drag and drop.** A plugin declares a list as orderable
   (data, as for any field). Core renders each row with a text field holding its position, 1 to n (there
   is no position 0), and one Update. A row whose number changed is moved to that position, and the
   others shift down to make room: moving the rule at 10 to 3 makes the rules at 3 to 9 become 4 to 10
   and puts the rule in the vacated 3. Every moved row lands exactly where it was asked to; the rows that
   did not move fill the other slots in their old order. When two moved rows ask for the same position,
   the one that was earlier in the list keeps it and the other is downshifted to the next free slot.
   Update is refused, and nothing written, if an entry is not a whole number or is below 1 or above n.
   It needs no script, so ADR 0013 is untouched. The CLI already works the same way (`oaken rule move
   --to PLACE`, 1 is first).
   *Cited:* "it's a plain position. it's functionally the equivalent of Move to index (3). (there is no
   rule 0, we're starting at one for the humans in the room)"; "you handle collisions by downshifting
   to make room."
5. **Reach.** A page is linked from its plugin's settings modal and its row on the stage page. A plugin
   adds no masthead entry; the masthead has one tab for each kind (ADR 0019). A kind's page lists its
   plugins one to a row, each with Reload and Settings buttons, and the settings modal is where the
   plugin's own endpoint is reached.
6. **A bad declaration falls back, never fails.** A `ui()` that raises or returns something malformed
   is logged, and the plugin's settings modal renders from its schema as today, without the page. The
   plugin stays healthy.
7. **It is additive within `PLUGIN_API_VERSION` 2** (ADR 0010) and ships in 0.2.0. Built after ADR 0017
   and before ADR 0016, whose rules wizard is its first user. The contract and operation snapshots are
   regenerated.

*Cited:* the owner's words above; Q1 to Q12 of `docs/bbq/2026-11-03-adr-webui-contract.md`.

## Alternatives

- **Data only, no endpoint (settings modal and wizard declarations only).** Smaller. Overruled by the
  owner, who wants a plugin to serve a form.
- **A restricted HTML fragment from the plugin.** More freedom, a sanitizer to trust. Overruled.
- **Raw routes under `/plugins/<name>/`.** A plugin could break the no-script CSP, ADR 0013 and CSRF.
  Overruled.
- **A static manifest file.** A second source of truth beside the config model. Overruled.
- **Panels, masthead entries and pages of free layout.** Out of scope; navigation would become contract.

## Consequences

- A third-party plugin gets a settings modal of its own and a form page with no change to oaken-core.
- Core gains a renderer for `UiSpec` (modal sections, pages) and one route family; the template's
  special cases (`rules`, `skip_rules`, `steps`) become declarations as each plugin moves, not in this
  change.
- ADR 0016's `rules.wizard` read operation is dropped: the rules triager declares its page.
- The rules plugin's page and wizard are a page like any other, so the console's new-rule page is no
  template of core's.

**Confirmed by:** snapshot tests; a test that a label with markup is escaped; a test that a page cannot
run another plugin's operation; a test that a malformed `ui()` falls back and logs.

**Revisit when:** a plugin needs a repeatable group of fields, a masthead entry, or content that is not
a form.

## History

- 2026-11-03: drafted from the owner's answers in a working session. Not ratified. Only the owner
  ratifies.
- 2026-11-03: ratified by the owner, who verified the built implementation works for 0.2.0.
- 2026-11-03 (same session, before ratification): the owner ruled rule order is a position field with
  Update, not drag and drop. Decision 4a added.
- 2026-11-03 (same session, before ratification): the owner ruled collisions downshift to make room.
  Decision 4a no longer refuses two rows asking for the same position. The earlier text refused it.
