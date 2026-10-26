"""Read the kernel's mount table into `MountPoint` values.

`/proc/self/mountinfo` is used instead of `/proc/mounts` because it is the
documented, complete format. Each line looks like this (see `man 5 proc`,
https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html):

    36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors=continue
    (1)(2)(3)  (4)   (5)   (6)        (7)     (8) (9)   (10)      (11)

    (4)  root: the folder of the source filesystem that is mounted (bind mounts)
    (5)  mount point
    (6)  per-mount options
    (7)  zero or more optional fields, ended by the "-" separator (8)
    (9)  filesystem type
    (10) source, such as a device
    (11) super options: options of the filesystem itself

`windows_names` is a super option, so both option lists are merged.
"""

import re
from pathlib import Path, PurePosixPath

from steamshare.core.mounts import MountPoint

MOUNTINFO_PATH = Path("/proc/self/mountinfo")

# Ends the optional fields, which can be any number of words.
OPTIONAL_FIELDS_SEPARATOR = "-"

# Field positions, counted from the start of the line or from the separator.
MOUNT_POINT_FIELD = 4
MOUNT_OPTIONS_FIELD = 5
FIRST_OPTIONAL_FIELD = 6
FILESYSTEM_TYPE_AFTER_SEPARATOR = 1
SOURCE_AFTER_SEPARATOR = 2
SUPER_OPTIONS_AFTER_SEPARATOR = 3

# The kernel writes a space, tab, newline, or backslash in a path as a
# three-digit octal escape, such as "\040" for a space.
OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def read_mount_table() -> list[MountPoint]:
    """Return the mounts visible to this process, in mount order."""
    return parse_mountinfo(MOUNTINFO_PATH.read_text(encoding="utf-8"))


def parse_mountinfo(text: str) -> list[MountPoint]:
    """Parse the text of a mountinfo file, keeping the line order."""
    return [_parse_line(line) for line in text.splitlines() if line.strip()]


def _parse_line(line: str) -> MountPoint:
    fields = line.split(" ")
    separator = fields.index(OPTIONAL_FIELDS_SEPARATOR, FIRST_OPTIONAL_FIELD)
    mount_options = fields[MOUNT_OPTIONS_FIELD].split(",")
    super_options = fields[separator + SUPER_OPTIONS_AFTER_SEPARATOR].split(",")
    return MountPoint(
        target=PurePosixPath(_decode_octal_escapes(fields[MOUNT_POINT_FIELD])),
        source=_decode_octal_escapes(fields[separator + SOURCE_AFTER_SEPARATOR]),
        filesystem_type=fields[separator + FILESYSTEM_TYPE_AFTER_SEPARATOR],
        options=frozenset(mount_options + super_options),
    )


def _decode_octal_escapes(field: str) -> str:
    return OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)
