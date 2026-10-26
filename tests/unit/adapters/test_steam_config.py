"""Tests for reading and rewriting Steam's `config.vdf`.

Steam owns this file, so a rewrite must be backed up first, must keep the
file's permissions, and must leave Steam's layout intact.
"""

from datetime import datetime
from pathlib import Path

import pytest

from steamshare.adapters.steam_config import (
    SteamConfigError,
    default_steam_root,
    read_steam_config,
    steam_config_path,
    write_steam_config,
)

NOW = datetime(2026, 10, 26, 18, 30, 5)
CONFIG_TEXT = '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t}\n}\n'


def test_the_default_steam_root_is_in_the_given_home() -> None:
    # install runs as root, so the home is the desktop user's, not root's.
    assert default_steam_root(Path("/home/player")) == Path("/home/player/.steam/steam")


def test_the_config_lives_in_the_config_folder_of_the_steam_root() -> None:
    assert steam_config_path(Path("/home/u/.steam/steam")) == Path(
        "/home/u/.steam/steam/config/config.vdf"
    )


def test_reads_and_parses_the_config(tmp_path: Path) -> None:
    path = tmp_path / "config.vdf"
    path.write_text(CONFIG_TEXT, encoding="utf-8")

    assert read_steam_config(path) == {"InstallConfigStore": {"Software": {}}}


def test_an_unparsable_config_is_reported_with_its_path(tmp_path: Path) -> None:
    path = tmp_path / "config.vdf"
    path.write_text('"InstallConfigStore"\n{\n', encoding="utf-8")

    with pytest.raises(SteamConfigError, match=str(path)):
        read_steam_config(path)


def test_writes_a_backup_then_the_new_config_keeping_the_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.vdf"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    path.chmod(0o775)  # what Steam leaves on the real file

    backup = write_steam_config(path, {"InstallConfigStore": {"Software": {"a": "1"}}}, NOW)

    assert backup == tmp_path / "config.vdf.bak-20261026-183005"
    assert backup.read_text(encoding="utf-8") == CONFIG_TEXT
    assert path.read_text(encoding="utf-8") == (
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"a"\t\t"1"\n\t}\n}\n'
    )
    assert path.stat().st_mode & 0o777 == 0o775
