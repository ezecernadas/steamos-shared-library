"""Tests for finding the desktop user behind `install`.

`install` runs under sudo, but the files must belong to the user who ran
sudo, not to root. The environment and the password database are injected.
"""

from pathlib import PurePosixPath

from steamshare.adapters.invoking_user import InvokingUser, resolve_invoking_user

HOMES = {0: "/root", 1000: "/home/player", 1001: "/home/deck"}


def home_of(user_id: int) -> str:
    return HOMES[user_id]


def test_under_sudo_the_user_is_the_one_who_ran_sudo() -> None:
    environment = {"SUDO_UID": "1000", "SUDO_GID": "1000", "SUDO_USER": "player"}

    user = resolve_invoking_user(
        environment, current_user_id=lambda: 0, current_group_id=lambda: 0, home_of=home_of
    )

    assert user == InvokingUser(user_id=1000, group_id=1000, home=PurePosixPath("/home/player"))


def test_without_sudo_the_user_is_the_current_one() -> None:
    user = resolve_invoking_user(
        {}, current_user_id=lambda: 1001, current_group_id=lambda: 1002, home_of=home_of
    )

    assert user == InvokingUser(user_id=1001, group_id=1002, home=PurePosixPath("/home/deck"))
