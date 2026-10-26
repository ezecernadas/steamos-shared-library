"""List the machine's NTFS partitions with `lsblk`.

`lsblk` reads the partition table and the filesystem signatures (through
libblkid) without root, so listing works for any user. Its JSON output nests
partitions under their disk as `children`; this adapter flattens that tree.
"""

import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath
from typing import Any

from steamshare.core.ntfs_partitions import NtfsPartition

# --bytes gives exact sizes instead of rounded text such as "96G".
# MOUNTPOINTS (util-linux 2.37 and later) lists every place a device is
# mounted; a device that is not mounted has a single null entry.
LSBLK_COMMAND = (
    "lsblk",
    "--json",
    "--bytes",
    "--output",
    "PATH,FSTYPE,SIZE,LABEL,UUID,MOUNTPOINTS",
)

# The filesystem type libblkid reads from the partition's own signature. It is
# "ntfs" whatever driver mounts it (ntfs3 or ntfs-3g), and also when unmounted.
NTFS_SIGNATURE = "ntfs"

# Runs a command and returns what it printed.
OutputRunner = Callable[[list[str]], str]


def run_for_output(command: list[str]) -> str:
    """Run a command, raising `subprocess.CalledProcessError` if it fails."""
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def read_ntfs_partitions(run: OutputRunner = run_for_output) -> list[NtfsPartition]:
    return parse_ntfs_partitions(run(list(LSBLK_COMMAND)))


def parse_ntfs_partitions(lsblk_json: str) -> list[NtfsPartition]:
    """Return the NTFS partitions in the order lsblk lists them (disk order)."""
    devices = json.loads(lsblk_json)["blockdevices"]
    return [
        _to_partition(device)
        for device in _flatten(devices)
        if device.get("fstype") == NTFS_SIGNATURE
    ]


def _flatten(devices: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for device in devices:
        yield device
        yield from _flatten(device.get("children", []))


def _to_partition(device: dict[str, Any]) -> NtfsPartition:
    mount_points = [point for point in device.get("mountpoints", []) if point]
    return NtfsPartition(
        device=PurePosixPath(device["path"]),
        # int() because some lsblk versions write numbers in JSON as strings.
        size_bytes=int(device["size"]),
        label=device.get("label"),
        uuid=device.get("uuid"),
        # The first entry is where the whole partition was mounted first;
        # later ones are usually bind mounts of folders inside it.
        mount_point=PurePosixPath(mount_points[0]) if mount_points else None,
    )
