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

# Returns the home folder of a user id.
HomeLookup = Callable[[int], str]


@dataclass(frozen=True)
class InvokingUser:
    user_id: int
    group_id: int
    home: PurePosixPath


def home_from_password_database(user_id: int) -> str:
    return pwd.getpwuid(user_id).pw_dir


def resolve_invoking_user(
    environment: Mapping[str, str] = os.environ,
    *,
    current_user_id: Callable[[], int] = os.getuid,
    current_group_id: Callable[[], int] = os.getgid,
    home_of: HomeLookup = home_from_password_database,
) -> InvokingUser:
    """Return the user who ran sudo, or the current user when not under sudo.

    The home comes from the password database, not from `$HOME`, which sudo
    may have reset to root's.
    """
    if SUDO_USER_ID_VARIABLE in environment:
        user_id = int(environment[SUDO_USER_ID_VARIABLE])
        group_id = int(environment[SUDO_GROUP_ID_VARIABLE])
    else:
        user_id = current_user_id()
        group_id = current_group_id()
    return InvokingUser(user_id=user_id, group_id=group_id, home=PurePosixPath(home_of(user_id)))
