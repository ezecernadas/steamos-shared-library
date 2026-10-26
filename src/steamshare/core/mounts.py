"""The mount table, as plain data, and the lookups `doctor` needs on it.

Why this matters: the shared library only works when the NTFS partition is
mounted with the right driver and options, and when `compatdata` and
`shadercache` are bind-mounted over it from ext4. Both facts live in the
kernel's mount table. The `mountinfo` adapter reads that table into
`MountPoint` values; this module only answers questions about them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

# Filesystem type names as the kernel reports them in the mount table.
NTFS3_FILESYSTEM = "ntfs3"  # the in-kernel NTFS driver this project relies on
# The ntfs-3g FUSE driver reports the generic FUSE block type, not "ntfs".
NTFS_3G_FILESYSTEM = "fuseblk"

# Mount option names, as they appear in the mount table.
READ_ONLY_OPTION = "ro"
# Makes ntfs3 refuse names Windows forbids (EINVAL), instead of writing them.
WINDOWS_NAMES_OPTION = "windows_names"

# ntfs3 has no Linux permissions on disk, so every file shows the owner and
# mode given at mount time. 022 lets only the owner write, as on ext4.
NTFS3_UMASK = "022"

# The first desktop user on most distros and on SteamOS (the `deck` user).
# Used in hints, where the real user is not known.
DEFAULT_USER_ID = 1000


def build_ntfs3_mount_options(user_id: int, group_id: int) -> str:
    """Return the options this project mounts the library partition with.

    The files belong to the desktop user, so Steam can write them without root
    (docs/PLAN.md, "NTFS mount unit").
    """
    return f"uid={user_id},gid={group_id},umask={NTFS3_UMASK},{WINDOWS_NAMES_OPTION}"


NTFS3_MOUNT_OPTIONS = build_ntfs3_mount_options(DEFAULT_USER_ID, DEFAULT_USER_ID)


@dataclass(frozen=True)
class MountPoint:
    """One entry of the mount table."""

    target: PurePosixPath
    source: str
    filesystem_type: str
    # Per-mount and filesystem (super block) options together, such as
    # "ro", "windows_names" or "uid=1000".
    options: frozenset[str]


def find_mount_containing(mounts: Iterable[MountPoint], path: PurePosixPath) -> MountPoint | None:
    """Return the mount that holds `path`: the one with the longest matching target."""
    containing = None
    for mount in mounts:
        if not _is_same_or_parent(mount.target, path):
            continue
        # `>=` so that a later mount on the same target wins: the mount table
        # lists mounts in order, and a later one hides the earlier one.
        if containing is None or len(mount.target.parts) >= len(containing.target.parts):
            containing = mount
    return containing


def find_mount_at(mounts: Iterable[MountPoint], path: PurePosixPath) -> MountPoint | None:
    """Return the mount whose target is exactly `path`, or None."""
    found = None
    for mount in mounts:
        # Keep looking after a match: a later mount on the same target hides
        # the earlier one.
        if mount.target == path:
            found = mount
    return found


def _is_same_or_parent(candidate: PurePosixPath, path: PurePosixPath) -> bool:
    return candidate == path or candidate in path.parents
