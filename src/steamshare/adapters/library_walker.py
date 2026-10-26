"""Walk the library folder and list what lives on the NTFS partition.

Two rules keep the walk on NTFS, where Windows will read the files:

- Symlinks are never followed. They are reported as entries; what they point
  to may be anywhere, including outside the library.
- Folders on a different device (`st_dev`) than the root are listed but never
  entered. That is how the bind-mounted ext4 `compatdata` and `shadercache`
  are skipped: Proton's `c:` names are expected there and harmless.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from steamshare.core.library_layout import LibraryEntry

# Returns the device ID of a path, without following a symlink at the end.
DeviceOf = Callable[[str], int]


def read_device_id(path: str) -> int:
    return os.lstat(path).st_dev


def walk_library(root: Path, device_of: DeviceOf = read_device_id) -> Iterator[LibraryEntry]:
    """Yield every file, folder, and symlink below `root`, sorted by name per folder.

    `device_of` is a parameter so tests can simulate a bind mount without
    mounting anything.
    """
    root_device = device_of(str(root))
    yield from _walk_folder(root, Path(root), root_device, device_of)


def _walk_folder(
    root: Path, folder: Path, root_device: int, device_of: DeviceOf
) -> Iterator[LibraryEntry]:
    with os.scandir(folder) as scanned:
        children = sorted(scanned, key=lambda child: child.name)
    for child in children:
        is_symlink = child.is_symlink()
        yield LibraryEntry(
            relative_path=PurePosixPath(Path(child.path).relative_to(root)),
            is_symlink=is_symlink,
        )
        if _should_enter(child, is_symlink, root_device, device_of):
            yield from _walk_folder(root, Path(child.path), root_device, device_of)


def _should_enter(
    child: os.DirEntry[str], is_symlink: bool, root_device: int, device_of: DeviceOf
) -> bool:
    if is_symlink or not child.is_dir(follow_symlinks=False):
        return False
    return device_of(child.path) == root_device
