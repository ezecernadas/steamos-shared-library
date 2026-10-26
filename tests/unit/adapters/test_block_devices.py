"""Tests for reading the NTFS partitions out of `lsblk --json`.

The fixture is the real output of `LSBLK_COMMAND` on the development machine:
snap loop devices, the disk itself (no filesystem), the EFI partition, a
partition with no filesystem, three NTFS partitions (Windows C:, the SteamLib
library mounted at /mnt/steamlib, the recovery partition), and the ext4 root
with the library's two bind mounts.
"""

from pathlib import Path, PurePosixPath

from steamshare.adapters.block_devices import parse_ntfs_partitions, read_ntfs_partitions
from steamshare.core.ntfs_partitions import NtfsPartition

FIXTURE = Path(__file__).parent / "fixtures" / "lsblk_dual_boot.json"


def test_keeps_only_the_ntfs_partitions_in_disk_order() -> None:
    partitions = parse_ntfs_partitions(FIXTURE.read_text(encoding="utf-8"))

    assert [str(partition.device) for partition in partitions] == [
        "/dev/nvme0n1p3",
        "/dev/nvme0n1p4",
        "/dev/nvme0n1p5",
    ]


def test_reads_size_label_uuid_and_mount_point() -> None:
    partitions = parse_ntfs_partitions(FIXTURE.read_text(encoding="utf-8"))

    assert partitions[1] == NtfsPartition(
        device=PurePosixPath("/dev/nvme0n1p4"),
        size_bytes=103078166528,
        label="SteamLib",
        uuid="1111AAAA2222BBBB",
        mount_point=PurePosixPath("/mnt/steamlib"),
    )


def test_an_unmounted_partition_without_label_has_neither() -> None:
    windows = parse_ntfs_partitions(FIXTURE.read_text(encoding="utf-8"))[0]

    assert windows.label is None
    assert windows.mount_point is None
    assert windows.uuid == "3333CCCC4444DDDD"


def test_accepts_sizes_written_as_strings_by_older_lsblk() -> None:
    text = (
        '{"blockdevices": [{"path": "/dev/sda1", "fstype": "ntfs", "size": "1024",'
        ' "label": null, "uuid": "AB", "mountpoints": [null]}]}'
    )

    [partition] = parse_ntfs_partitions(text)

    assert partition.size_bytes == 1024


def test_reads_lsblk_through_the_given_runner() -> None:
    commands: list[list[str]] = []

    def run_lsblk(command: list[str]) -> str:
        commands.append(command)
        return FIXTURE.read_text(encoding="utf-8")

    partitions = read_ntfs_partitions(run_lsblk)

    assert len(partitions) == 3
    assert commands[0][0] == "lsblk"
    assert "--json" in commands[0]
