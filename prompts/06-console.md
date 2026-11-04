# Step 6: the console

Goal: a console you cannot lock yourself out of.

Lift from oaken-mail: `plugins/ui.py` (`check_ui`, `reposition`), `api/guard.py`, the Jinja templates
for config modals, plugin pages, the pipeline and the stage views. Strip mail copy from the templates.
Read `docs/origin/0018-a-web-ui-contract-for-plugins.md`.

Build:
1. The console is core's. It is not a plugin. It cannot be switched off.
2. It binds loopback only. The bind is fixed. No login from localhost. Nothing configures a wider bind.
3. Guard it anyway. Check Host and Origin. Put a CSRF token on every mutation.
4. It serves config, plugins, queues, health, and every plugin's `UiSpec`.
5. Core serves a `UiSpec` as JSON. It also serves a minimal Jinja fallback.
   A plugin with no `UiSpec` gets a modal built from its config schema.
6. `BaseAuth` guards the API, MCP and gateways. `remote_ok` lives there. It does not touch the console.

Done when:
- Disabling every plugin leaves the console up.
- A request with a foreign Host or Origin is refused.
- A plugin's `UiSpec` renders as a form and as JSON.
- A bad `UiSpec` falls back to the schema-built modal.
