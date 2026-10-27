"""Put a downloaded release where it stays: the folder the one-line install manages.

`curl -fsSL <release>/install.sh | sudo bash` (release/install.sh) downloads
the source archive of a release into a temporary folder, and
`steamshare.release_setup` copies it here before running `install` from the
copy. It must be a lasting place: the watch service runs the launcher by its
absolute path, and the `steamshare` command links to it.

A folder placed here holds a marker file with its version, so a release
install can be told from anything else with the same name, such as a clone:
only a marked folder is ever replaced. Running the one-liner again replaces
the release with the new one:

1. the release is copied to `.<name>.new` beside the destination, on the
   same filesystem, so the next steps are renames;
2. the old release, if any, is renamed to `.<name>.old`;
3. the new one is renamed into place, and the old one deleted.

Interrupted, it leaves at most a leftover `.new` or `.old`, which the next run
deletes first. Between the two renames the destination is briefly missing;
an interrupted run there is mended by running the one-liner again.
"""

import os
import shutil
from pathlib import Path

from steamshare.adapters.folder_contents import remove_tree
from steamshare.adapters.safe_writes import ChangeOwner

# Where the release goes; `steamshare.release_setup` chooses between them.
SYSTEM_INSTALL_FOLDER = Path("/opt/steamshare")
# The user's own data (XDG Base Directory spec), for when /opt is on a
# read-only filesystem, as on SteamOS.
USER_INSTALL_FOLDER_IN_HOME = Path(".local") / "share" / "steamshare"

# Holds the version; its presence is what makes the folder a release install.
RELEASE_MARKER_FILE_NAME = ".steamshare-release"

STAGING_PREFIX = "."
STAGING_SUFFIX = ".new"
RETIRED_SUFFIX = ".old"


class ForeignFolderError(Exception):
    """The destination exists and is not a release install, so it is left alone."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists and is not a steamshare release install")
        self.path = path


def is_release_install(folder: Path) -> bool:
    return (folder / RELEASE_MARKER_FILE_NAME).is_file()


def installed_release_version(folder: Path) -> str | None:
    """Return the version of the release installed in the folder, or None."""
    if not is_release_install(folder):
        return None
    return (folder / RELEASE_MARKER_FILE_NAME).read_text().strip()


def place_release(
    source: Path,
    destination: Path,
    *,
    version: str,
    owner: tuple[int, int] | None,
    change_owner: ChangeOwner = os.lchown,
) -> None:
    """Copy the extracted release to `destination`, replacing a previous release.

    `owner` is the (user id, group id) every file is given, for a folder in
    the user's home; None keeps them root's. `change_owner` must not follow
    links (`os.lchown`), so a link in the release never hands over its target.
    """
    if destination.exists() and not is_release_install(destination):
        raise ForeignFolderError(destination)
    staging = destination.with_name(f"{STAGING_PREFIX}{destination.name}{STAGING_SUFFIX}")
    retired = destination.with_name(f"{STAGING_PREFIX}{destination.name}{RETIRED_SUFFIX}")
    _remove_leftover(staging)
    _remove_leftover(retired)

    shutil.copytree(source, staging, symlinks=True)
    (staging / RELEASE_MARKER_FILE_NAME).write_text(f"{version}\n")
    if owner is not None:
        _give_tree(staging, *owner, change_owner=change_owner)

    if destination.exists():
        os.rename(destination, retired)
    os.rename(staging, destination)
    _remove_leftover(retired)


def _remove_leftover(path: Path) -> None:
    # A link is removed itself, never followed (`remove_tree` refuses one).
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        remove_tree(path)


def _give_tree(root: Path, user_id: int, group_id: int, *, change_owner: ChangeOwner) -> None:
    change_owner(root, user_id, group_id)
    for folder, subfolders, files in os.walk(root):
        for name in [*subfolders, *files]:
            change_owner(Path(folder) / name, user_id, group_id)
