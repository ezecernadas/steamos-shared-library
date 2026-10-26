"""Measure and delete the ext4 folders `uninstall --purge` removes.

Those folders hold Proton prefixes, and every prefix holds links that leave
it: `dosdevices/z:` points at `/`, and `dosdevices/c:` at the prefix's own
drive. So nothing here ever follows a link: a size counts the link, not what
it points to, and a delete removes the link, not its target.
"""

import os
import shutil
from pathlib import Path


def folder_size(path: Path) -> int:
    """Return the bytes the folder's files and links take, without following links."""
    total = 0
    # followlinks=False (the default) keeps the walk out of linked folders.
    for folder, subfolders, files in os.walk(path, followlinks=False):
        for name in [*subfolders, *files]:
            # lstat describes a link itself, not its target.
            total += (Path(folder) / name).lstat().st_size
    return total


def subfolder_names(path: Path) -> list[str]:
    """Return the names of the folder's subfolders, links excluded, sorted."""
    if not path.is_dir():
        return []
    return sorted(
        entry.name for entry in path.iterdir() if entry.is_dir() and not entry.is_symlink()
    )


def remove_tree(path: Path) -> None:
    """Delete the folder and everything in it, never anything a link inside points to.

    shutil.rmtree unlinks every link it meets instead of descending into it,
    and on Linux it uses the file-descriptor based variant
    (`shutil.rmtree.avoids_symlink_attacks`), which also resists a folder
    being swapped for a link mid-way. When `path` itself is a link it raises
    OSError rather than delete the target's contents.
    """
    shutil.rmtree(path)
