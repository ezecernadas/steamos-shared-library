"""Find the desktop user on whose behalf a command runs.

`install` runs as root through sudo, but the NTFS files and the ext4 folders
must belong to the user who runs Steam. sudo records that user's ids in
`SUDO_UID` and `SUDO_GID`; without sudo, the current user is the one.
"""

import os
import pwd
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

SUDO_USER_ID_VARIABLE = "SUDO_UID"
SUDO_GROUP_ID_VARIABLE = "SUDO_GID"


@dataclass(frozen=True)
class UserAccount:
    """What the password database says about a user id."""

    name: str
    home: str


# Returns the account of a user id.
AccountLookup = Callable[[int], UserAccount]


@dataclass(frozen=True)
class InvokingUser:
    user_id: int
    group_id: int
    # The login name, which `systemctl --user --machine=<name>@` needs.
    name: str
    home: PurePosixPath


def account_from_password_database(user_id: int) -> UserAccount:
    entry = pwd.getpwuid(user_id)
    return UserAccount(name=entry.pw_name, home=entry.pw_dir)


def resolve_invoking_user(
    environment: Mapping[str, str] = os.environ,
    *,
    current_user_id: Callable[[], int] = os.getuid,
    current_group_id: Callable[[], int] = os.getgid,
    account_of: AccountLookup = account_from_password_database,
) -> InvokingUser:
    """Return the user who ran sudo, or the current user when not under sudo.

    The name and home come from the password database, by user id: `$HOME`
    may have been reset to root's by sudo, and the id is what files are owned by.
    """
    if SUDO_USER_ID_VARIABLE in environment:
        user_id = int(environment[SUDO_USER_ID_VARIABLE])
        group_id = int(environment[SUDO_GROUP_ID_VARIABLE])
    else:
        user_id = current_user_id()
        group_id = current_group_id()
    account = account_of(user_id)
    return InvokingUser(
        user_id=user_id, group_id=group_id, name=account.name, home=PurePosixPath(account.home)
    )
