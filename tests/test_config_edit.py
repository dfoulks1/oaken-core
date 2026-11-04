# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken_core.config_edit import get_table, refuse_secrets, remove_table, set_table
from oaken_core.errors import ConfigError

FILE = """\
# my oaken
[api]
port = 8123  # keep this

[plugins.gmail]
# which mailbox
enabled = true
extra = "x"

[plugins.imap]
host = "imap.example.com"
"""


def test_a_value_changes_and_every_comment_and_other_table_stays() -> None:
    out = set_table(FILE, ["plugins", "gmail"], {"enabled": False, "extra": "x"})
    assert "enabled = false" in out
    assert "# my oaken" in out and "# keep this" in out and "# which mailbox" in out
    assert get_table(out, ["plugins", "imap"]) == {"host": "imap.example.com"}
    assert get_table(out, ["api"]) == {"port": 8123}


def test_keys_not_given_are_removed_and_new_ones_added() -> None:
    out = set_table(FILE, ["plugins", "gmail"], {"enabled": True, "capabilities": ["sync.pull"]})
    assert get_table(out, ["plugins", "gmail"]) == {"enabled": True, "capabilities": ["sync.pull"]}


def test_a_missing_table_and_its_parents_are_created() -> None:
    out = set_table("", ["plugins", "mbox"], {"path": "mail.mbox"})
    assert get_table(out, ["plugins", "mbox"]) == {"path": "mail.mbox"}


def test_other_tables_are_byte_for_byte_the_same() -> None:
    out = set_table(FILE, ["plugins", "gmail"], {"enabled": False})
    assert '[plugins.imap]\nhost = "imap.example.com"\n' in out
    assert out.startswith("# my oaken\n[api]\nport = 8123  # keep this\n")


def test_a_secret_is_refused_in_any_table() -> None:
    with pytest.raises(ConfigError, match="keyring"):
        set_table(FILE, ["plugins", "imap"], {"host": "h", "password": "hunter2"})
    with pytest.raises(ConfigError, match="keyring"):
        refuse_secrets({"oauth": {"client_secret": "x"}})


def test_invalid_toml_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        set_table("[oops", ["plugins", "x"], {})


def test_a_key_where_a_table_belongs_is_refused() -> None:
    with pytest.raises(ConfigError, match="not a table"):
        set_table("plugins = { gmail = 1 }\n", ["plugins", "gmail"], {"a": 1})


def test_remove_table_drops_only_that_table() -> None:
    out = remove_table(FILE, ["plugins", "gmail"])
    assert get_table(out, ["plugins", "gmail"]) == {}
    assert get_table(out, ["plugins", "imap"]) == {"host": "imap.example.com"}
    assert remove_table(FILE, ["plugins", "nope"]).count("[plugins.gmail]") == 1


from oaken_core.config_edit import (  # noqa: E402
    edited_table,
    get_table_text,
    set_table_text,
    snippet_values,
)

HOSTED = """\
# mine
[api]
port = 8123  # keep

[plugins.gmail]
# which one
enabled = true
capabilities = ["sync.pull"]
account = "default"  # name

[plugins.imap]
host = "h"
"""
GMAIL = ["plugins", "gmail"]


def test_the_text_for_an_editor_keeps_its_comments_and_leaves_out_the_hosts_keys() -> None:
    assert get_table_text(HOSTED, GMAIL) == '# which one\naccount = "default"  # name\n'
    assert get_table_text(HOSTED, ["plugins", "nope"]) == ""


def test_saving_text_keeps_what_was_typed_and_the_hosts_keys_as_saved() -> None:
    out = set_table_text(HOSTED, GMAIL, '# mine now\naccount = "work"  # x\nwidth = 3\n')
    assert get_table(out, GMAIL) == {
        "account": "work",
        "width": 3,
        "enabled": True,
        "capabilities": ["sync.pull"],
    }
    assert "# mine now" in out and "# x" in out and "# which one" not in out
    assert out.startswith("# mine\n[api]\nport = 8123  # keep\n")
    assert get_table(out, ["plugins", "imap"]) == {"host": "h"}


def test_saving_text_takes_a_header_and_the_hosts_keys_can_be_set_or_unset() -> None:
    out = set_table_text(
        HOSTED,
        GMAIL,
        '[plugins.gmail]\naccount = "a"\n',
        config={"enabled": False},
        unset=["capabilities"],
    )
    assert get_table(out, GMAIL) == {"account": "a", "enabled": False}


def test_text_for_another_table_or_with_a_secret_is_refused() -> None:
    with pytest.raises(ConfigError, match="only the settings"):
        set_table_text(HOSTED, GMAIL, "[plugins.gmail]\na = 1\n[api]\nport = 1\n")
    with pytest.raises(ConfigError, match="only the settings"):
        snippet_values("[api]\nport = 1\n", GMAIL)
    with pytest.raises(ConfigError, match="keyring"):
        set_table_text(HOSTED, GMAIL, 'password = "x"\n')


def test_the_edited_table_is_what_a_save_would_leave() -> None:
    assert edited_table(HOSTED, GMAIL, snippet="account = 'w'\n", unset=["enabled"]) == {
        "account": "w",
        "capabilities": ["sync.pull"],
    }
    assert edited_table(HOSTED, GMAIL, config={"x": 1}) == {
        "enabled": True,
        "capabilities": ["sync.pull"],
        "account": "default",
        "x": 1,
    }
    assert edited_table(HOSTED, GMAIL, config={"x": 1}, replace=True) == {"x": 1}


@pytest.mark.parametrize(
    "key",
    ["password", "imap_password", "api_key", "apiKey", "access_token", "token", "client_secret",
     "oauthSecret", "credential", "passphrase"],
)  # fmt: skip
def test_a_key_that_names_a_secret_is_refused(key: str) -> None:
    with pytest.raises(ConfigError, match="keyring"):
        refuse_secrets({key: "x"})


@pytest.mark.parametrize(
    "key",
    ["chunk_tokens", "enrich_max_input_tokens", "max_tokens",
     "client_secret_file", "token_file", "secret_path", "api_key_name", "host", "port"],
)  # fmt: skip
def test_a_key_that_only_counts_or_points_at_one_is_not_a_secret(key: str) -> None:
    refuse_secrets({key: 1})  # no error: these are ordinary settings
