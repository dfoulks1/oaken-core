from oaken_core.settings import Settings


def test_starter_is_written_once_and_is_valid(tmp_path):
    path = tmp_path / "config.toml"
    assert Settings.write_starter(path) is True
    assert Settings.load(path) == Settings.from_text("")  # all commented: the defaults

    path.write_text("[log]\nlevel = 'DEBUG'\n")
    assert Settings.write_starter(path) is False  # never overwrites
    assert Settings.load(path).log.level == "DEBUG"
