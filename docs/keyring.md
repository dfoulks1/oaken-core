+++
title = "Store credentials in your keyring"
subtitle = "Where Oaken keeps a plugin's passwords and tokens"
lead = "Core never writes a password or token to a file. A plugin asks your operating system's keyring to keep it. This page shows the entry names, the commands, and what happens when there is no keyring."
badges = [["text-bg-warning", "Alpha"]]
group = "start"
order = 11
description = "How Oaken Core uses the OS keyring: the service and entry names, oaken secret set, status and delete, what config.toml refuses, and what the no-keyring error means."
+++

## What the keyring is

An encrypted store run by your desktop session. Programs ask it to save and fetch small secrets.
On Linux it is usually GNOME Keyring or KWallet (Secret Service); on macOS, Keychain; on Windows,
Credential Locker. Your login password normally unlocks it.

Core uses the Python `keyring` package. It imports it when a secret is first stored or read, so
core runs without a keyring until then. If the package is missing, the error says
`no keyring is installed (pip install keyring); secrets need one`.

## What is stored there

Each secret is one entry, found by a **service** and a **user** name.

| Part | Value |
| --- | --- |
| Service | The product's app name: `oaken` for core. |
| User | `<kind>:<name>/<key>` |

`<kind>:<name>` is the plugin instance (`provider.socket` is kind `provider`, name `socket`).
`<key>` is what the plugin calls the secret, such as `password`. Each plugin reaches only its own
entries, through `ctx.secrets` (`get`, `set`, `delete`). Two instances of one plugin never see
each other's. A product built on core has its own app name, so its entries are filed apart.

## Where secrets are refused

A secret never goes in `config.toml`. Core guesses from the words of a key: `password`, `passwd`,
`passphrase`, `secret`, `token`, `apikey`, `api_key` and `credential(s)` are refused. A key that
ends in `file`, `path`, `dir`, `folder`, `name`, `url` or `id` is allowed, so `client_secret_file`
passes (it names where a secret is kept). So does `chunk_tokens`: `tokens` is a count, not `token`.

The check runs on every door that writes the file:

- `oaken config validate` and `oaken config save` (whole file).
- `oaken plugin add --config` and the plugin forms in the console.
- The console's `/config` editor.

The error reads: `'password' looks like a secret, and secrets are never stored in config.toml; they
go in the OS keyring`.

A secret is also never logged, traced or written to an operation's audit row. The input is a
`SecretStr`, masked wherever it is dumped.

## Store a credential

Plugins often ship their own sign-in command. Core has three generic ones:

```bash
oaken secret set --kind provider --name socket --key password   # asks, no echo
oaken secret status --kind provider --name socket --key password   # stored: true or false
oaken secret delete --kind provider --name socket --key password
```

The value is asked for on a terminal without echo. It is never an argument. `set` is a change, so
without `--yes` it shows what would happen and exits 5. `set` refuses a plugin that is not loaded.
`status` never prints the value. None of the three is offered over MCP.

## When there is no keyring

Core stops that call with `the OS keyring failed: <ErrorName>`. It does not fall back to a file,
and it does not print the value. Usual causes:

- **The keyring is locked.** Unlock it; your login password opens it.
- **No Secret Service is running.** Install and start one (for example `gnome-keyring`).
- **No desktop session**, as over SSH or in a container. Run the command from your desktop session,
  or start one with `dbus-run-session` and unlock a keyring inside it.

`MemorySecrets` in `repo:src/oaken_core/secrets.py` keeps secrets in process memory for tests. A
product can pass its own `SecretStore` (`get`, `set`, `delete`). Code: `repo:src/oaken_core/secrets.py`.
