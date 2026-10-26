"""Tests for parsing the kernel's mount table (`/proc/self/mountinfo`).

The fixture lines mirror the real setup: the library partition on ntfs3 with
windows_names, and `compatdata` bind-mounted from an ext4 home folder.
"""

from pathlib import PurePosixPath

from steamshare.adapters.mountinfo import parse_mountinfo

ROOT_LINE = "26 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw,errors=remount-ro"
NTFS3_LINE = (
    "512 26 259:5 / /mnt/steamlib rw,relatime shared:300 - ntfs3 /dev/nvme0n1p5 "
    "rw,uid=1000,gid=1000,dmask=0022,fmask=0022,windows_names,acl,iocharset=utf8,prealloc"
)
BIND_LINE = (
    "520 512 259:2 /home/player/.local/share/steam-shared/compatdata "
    "/mnt/steamlib/SteamLibrary/steamapps/compatdata rw,relatime shared:1 "
    "- ext4 /dev/nvme0n1p2 rw,errors=remount-ro"
)
SPACE_LINE = (
    "530 26 259:6 / /mnt/Steam\\040Library ro,nosuid master:7 - ntfs3 /dev/sdb1 rw,windows_names"
)
NO_OPTIONAL_FIELDS_LINE = "40 26 0:30 / /tmp rw - tmpfs tmpfs rw,size=100k"


def test_parses_an_ntfs3_mount_and_merges_super_options() -> None:
    [mount] = parse_mountinfo(NTFS3_LINE)

    assert mount.target == PurePosixPath("/mnt/steamlib")
    assert mount.source == "/dev/nvme0n1p5"
    assert mount.filesystem_type == "ntfs3"
    # "relatime" is a per-mount option; "windows_names" a super option.
    assert {"rw", "relatime", "windows_names", "uid=1000"} <= mount.options


def test_parses_a_bind_mount_from_ext4() -> None:
    [mount] = parse_mountinfo(BIND_LINE)

    assert mount.target == PurePosixPath("/mnt/steamlib/SteamLibrary/steamapps/compatdata")
    assert mount.filesystem_type == "ext4"


def test_decodes_octal_escapes_in_paths() -> None:
    [mount] = parse_mountinfo(SPACE_LINE)

    assert mount.target == PurePosixPath("/mnt/Steam Library")
    assert "ro" in mount.options


def test_parses_lines_without_optional_fields() -> None:
    [mount] = parse_mountinfo(NO_OPTIONAL_FIELDS_LINE)

    assert mount.filesystem_type == "tmpfs"
    assert mount.target == PurePosixPath("/tmp")


def test_keeps_the_table_order_and_skips_blank_lines() -> None:
    text = "\n".join([ROOT_LINE, NTFS3_LINE, "", BIND_LINE]) + "\n"

    targets = [str(mount.target) for mount in parse_mountinfo(text)]

    assert targets == ["/", "/mnt/steamlib", "/mnt/steamlib/SteamLibrary/steamapps/compatdata"]
