# SPDX-License-Identifier: Apache-2.0
"""Edit one table of ``config.toml`` and leave the rest of the file alone.

The whole-file editor replaces the text. A form for one plugin cannot: it knows only that plugin's
keys, and the comments, ordering and other tables around them are the person's. ``tomlkit`` keeps
all of that, so this module takes the file's text and returns the new text, touching only the
table it was asked about. It does no I/O and no validation of the result: the caller validates the
whole file through ``Settings`` before it writes anything.
"""

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import tomlkit
from tomlkit.container import Container
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table

from oaken_core.errors import ConfigError

# Keys of a ``[plugins.<name>]`` table that belong to the host, not the plugin (see
# ``PluginHost``): a form edits them with its own controls, so the plugin's text leaves them out.
HOST_KEYS = ("enabled", "capabilities", "when", "unless")

# A secret never goes in config.toml: it lives in the OS keyring. A form that tries to save one
# here is refused, not trusted to be careful. The guess is made on the words of the key, so that
# ``password``, ``api_key`` and ``access_token`` are refused while ``chunk_tokens`` (a count) and
# ``client_secret_file`` (where a secret is kept, not the secret) are not.
_SECRET_WORDS = frozenset(
    {"password", "passwd", "passphrase", "secret", "token", "apikey", "credential", "credentials"}
)
_WHERE_IT_LIVES = frozenset({"file", "path", "dir", "folder", "name", "url", "id"})


def _words(key: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)  # camelCase -> camel_Case
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def looks_secret(key: str) -> bool:
    words = _words(key)
    if not words or words[-1] in _WHERE_IT_LIVES:
        return False
    return any(w in _SECRET_WORDS for w in words) or any(
        a == "api" and b == "key" for a, b in zip(words, words[1:], strict=False)
    )


def _parse(text: str) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(text)
    except TOMLKitError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from None


def _table(doc: Container | Mapping[str, Any], path: Sequence[str]) -> Mapping[str, Any] | None:
    node: Any = doc
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


def refuse_secrets(values: Mapping[str, Any]) -> None:
    """Raise ``ConfigError`` if any key looks like a secret (searched through nested tables)."""
    for key, value in values.items():
        if looks_secret(key):
            raise ConfigError(
                f"{key!r} looks like a secret, and secrets are never stored in config.toml; "
                "they go in the OS keyring (the plugin's own auth command stores them there)"
            )
        if isinstance(value, Mapping):
            refuse_secrets(value)


def get_table(text: str, path: Sequence[str]) -> dict[str, Any]:
    """The table at ``path`` as plain data (empty if the file has none)."""
    found = _table(_parse(text), path)
    return cast(dict[str, Any], tomlkit.item(found).unwrap()) if found is not None else {}


def get_table_text(text: str, path: Sequence[str]) -> str:
    """The table at ``path`` as TOML text for an editor: its own comments kept, without its header
    and without the host's keys (``HOST_KEYS``). Empty if the file has no such table."""
    found = _table(_parse(text), path)
    if not isinstance(found, Table):
        return ""
    body = _parse(found.as_string())
    for key in HOST_KEYS:
        body.pop(key, None)
    return tomlkit.dumps(body).strip("\n") + "\n" if len(body) else ""


def _snippet_table(snippet: str, path: Sequence[str]) -> Table:
    """The table a person typed: with its ``[plugins.x]`` header, or just the keys under it."""
    doc = _parse(snippet)
    found = _table(doc, path)
    if found is None:
        if any(isinstance(v, Mapping) for v in doc.values()):
            raise ConfigError(f"only the settings of [{'.'.join(path)}] go here")
        found = _table(_parse(f"[{'.'.join(path)}]\n{snippet}"), path)
    else:
        only: Any = tomlkit.item(found).unwrap()
        for part in reversed(path):
            only = {part: only}
        if tomlkit.item(doc).unwrap() != only:  # the header, and some other table beside it
            raise ConfigError(f"only the settings of [{'.'.join(path)}] go here")
    if not isinstance(found, Table):
        raise ConfigError(f"[{'.'.join(path)}] is not a table")
    return found


def snippet_values(snippet: str, path: Sequence[str]) -> dict[str, Any]:
    """The settings in text a person typed for the table at ``path``, as plain data."""
    values: dict[str, Any] = tomlkit.item(_snippet_table(snippet, path)).unwrap()
    return values


def _carry_host_keys(saved: Mapping[str, Any] | None, new: Any, unset: Sequence[str]) -> None:
    for key in HOST_KEYS:
        if saved is not None and key in saved and key not in new and key not in unset:
            new[key] = tomlkit.item(saved[key]).unwrap()


def edited_table(
    text: str,
    path: Sequence[str],
    *,
    snippet: str | None = None,
    config: Mapping[str, Any] | None = None,
    unset: Sequence[str] = (),
    replace: bool = False,
) -> dict[str, Any]:
    """What the table at ``path`` would hold after an edit, as plain data. ``snippet`` (an editor's
    text) replaces the plugin's own keys and keeps the host's; otherwise ``config`` is merged onto
    the saved table, or is the whole table with ``replace``. ``unset`` keys are then removed."""
    saved = get_table(text, path)
    if snippet is not None:
        table = snippet_values(snippet, path)
        _carry_host_keys(saved, table, unset)
        table.update(config or {})
    elif replace:
        table = dict(config or {})
    else:
        table = {**saved, **(config or {})}
    for key in unset:
        table.pop(key, None)
    return table


def set_table_text(
    text: str,
    path: Sequence[str],
    snippet: str,
    *,
    config: Mapping[str, Any] | None = None,
    unset: Sequence[str] = (),
) -> str:
    """``text`` with the table at ``path`` replaced by what a person typed (``snippet``), comments
    and all. The host's keys (``HOST_KEYS``) stay as saved unless ``config`` or ``unset`` names
    them. Everything outside the table is untouched."""
    doc = _parse(text)
    new = _snippet_table(snippet, path)
    refuse_secrets(tomlkit.item(new).unwrap())
    refuse_secrets(config or {})
    _carry_host_keys(_table(doc, path), new, unset)
    for key, value in (config or {}).items():
        new[key] = value
    for key in unset:
        if key in new:
            del new[key]
    parent = _parent(doc, path)
    parent[path[-1]] = new
    return tomlkit.dumps(doc)


def _parent(doc: Any, path: Sequence[str]) -> Any:
    parent: Any = doc
    for part in path[:-1]:
        if part not in parent:
            parent[part] = tomlkit.table(is_super_table=True)
        parent = parent[part]
    return parent


def _aot(rows: Sequence[Mapping[str, Any]]) -> Any:
    """A list of tables as ``[[...]]`` entries, a nested table on one line (``when = { ... }``)."""
    aot = tomlkit.aot()
    for row in rows:
        table = tomlkit.table()
        for name, value in row.items():
            if isinstance(value, Mapping):
                inline = tomlkit.inline_table()
                inline.update(dict(value))
                value = inline
            table.add(name, value)
        aot.append(table)
    return aot


def set_table(text: str, path: Sequence[str], values: Mapping[str, Any]) -> str:
    """``text`` with the table at ``path`` holding exactly ``values``. Keys already there keep
    their comments and place, keys not in ``values`` are removed, new ones are appended, and the
    table is created (with its parents) if the file has none. Everything outside it is untouched."""
    refuse_secrets(values)
    doc = _parse(text)
    parent = _parent(doc, path)
    leaf = path[-1]
    if leaf not in parent:
        parent[leaf] = tomlkit.table()
    table = parent[leaf]
    if not isinstance(table, Table):
        raise ConfigError(f"[{'.'.join(path)}] is not a table in config.toml")
    for key in [k for k in table if k not in values]:
        del table[key]
    for key, value in values.items():
        is_rows = isinstance(value, list) and value and all(isinstance(v, Mapping) for v in value)
        if is_rows and key in table:  # an array of tables is replaced, never merged
            del table[key]
        table[key] = _aot(value) if is_rows else value
    return tomlkit.dumps(doc)


def remove_table(text: str, path: Sequence[str]) -> str:
    """``text`` without the table at ``path`` (unchanged if it is not there)."""
    doc = _parse(text)
    parent: Any = _table(doc, path[:-1]) if len(path) > 1 else doc
    if parent is not None and path[-1] in parent:
        del parent[path[-1]]
    return tomlkit.dumps(doc)


def save_table(config_path: Path, path: Sequence[str], values: Mapping[str, Any]) -> None:
    """Write the table at ``path`` into ``config_path`` (made if absent), atomically, leaving the rest
    of the file as it was. Secrets are refused (``set_table``)."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    updated = set_table(text, path, values)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(config_path.name + f".{os.getpid()}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, config_path)
