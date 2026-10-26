"""Tests for finding the user's config folder the way the XDG spec says."""

from pathlib import Path

from steamshare.adapters.xdg_directories import user_config_home


def test_the_config_folder_defaults_to_dot_config_in_the_home() -> None:
    assert user_config_home({}, Path("/home/u")) == Path("/home/u/.config")


def test_the_config_folder_follows_xdg_config_home() -> None:
    assert user_config_home({"XDG_CONFIG_HOME": "/data/config"}, Path("/home/u")) == Path(
        "/data/config"
    )


def test_an_empty_xdg_config_home_counts_as_unset() -> None:
    # The spec says an empty value means "use the default".
    assert user_config_home({"XDG_CONFIG_HOME": ""}, Path("/home/u")) == Path("/home/u/.config")
