"""The step between the one-line install's download and `install`.

    curl -fsSL <release>/install.sh | sudo bash [-s -- <install options>]

release/install.sh only downloads the release's source archive, extracts it
to a temporary folder, and runs this module from there with the system's
python3:

    python3 -m steamshare.release_setup <extracted folder> <version> [<install options>]

It holds the decisions, so the shell holds none (CONTRIBUTING.md, rule 2):

1. It refuses root without sudo: the files must belong to the desktop user.
2. It chooses a lasting folder (`choose_install_folder`): /opt/steamshare,
   or ~/.local/share/steamshare where /opt is on a read-only filesystem, as
   on SteamOS. The watch service and the `steamshare` command point at the
   launcher there, so the temporary folder would not do.
3. It places the release there (`adapters.release_install`), replacing a
   previous release, so running the one-liner again updates.
4. It hands over to `<folder>/steamshare install <install options>`, which
   asks its questions and shows its summary as usual.

With `--dry-run` among the install options nothing is placed: install's dry
run runs from the temporary folder instead, and says so.
"""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from steamshare.adapters.command_link import is_on_read_only_filesystem
from steamshare.adapters.invoking_user import InvokingUser, resolve_invoking_user
from steamshare.adapters.launcher_location import LAUNCHER_FILE_NAME
from steamshare.adapters.release_install import (
    RELEASE_MARKER_FILE_NAME,
    SYSTEM_INSTALL_FOLDER,
    USER_INSTALL_FOLDER_IN_HOME,
    ForeignFolderError,
    installed_release_version,
    place_release,
)
from steamshare.adapters.safe_writes import ChangeOwner
from steamshare.adapters.unit_installer import create_owned_directories
from steamshare.cli.shared import ROOT_USER_ID, SUDO_INSTALL_COMMAND, CommandError

EXIT_OK = 0
EXIT_FAILURE = 1

INSTALL_COMMAND = "install"
DRY_RUN_FLAG = "--dry-run"


class ReleaseSetupError(CommandError):
    """An expected failure before install starts, with what to do next."""


@dataclass(frozen=True)
class InstallFolder:
    path: Path
    # The user the folder is given to, when it is in the user's home; None
    # for the system folder, which stays root's.
    owner: InvokingUser | None


# Replaces this process with the launcher, given the arguments after its path.
StartLauncher = Callable[[Path, list[str]], None]


@dataclass(frozen=True)
class System:
    """Every effect release setup has on the machine, replaceable in tests."""

    invoking_user: InvokingUser
    system_folder: Path
    is_read_only: Callable[[Path], bool]
    # Must not follow links (`os.lchown`).
    change_owner: ChangeOwner
    start_launcher: StartLauncher


def choose_install_folder(
    user: InvokingUser, system_folder: Path, *, is_read_only: Callable[[Path], bool]
) -> InstallFolder:
    """Return the system folder when its parent can be written, else the user's.

    The same rule as the `steamshare` command's link (`cli.command_link_setup`).
    """
    parent = system_folder.parent
    if parent.is_dir() and not is_read_only(parent):
        return InstallFolder(path=system_folder, owner=None)
    return InstallFolder(path=Path(user.home) / USER_INSTALL_FOLDER_IN_HOME, owner=user)


def release_setup(source: Path, version: str, install_arguments: list[str], system: System) -> int:
    """Place the release and start install from it; return only on failure, or in tests."""
    try:
        launcher = _prepare(source, version, install_arguments, system)
    except CommandError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    system.start_launcher(launcher, [INSTALL_COMMAND, *install_arguments])
    return EXIT_OK


def _prepare(source: Path, version: str, install_arguments: list[str], system: System) -> Path:
    """Return the launcher install runs from, after placing the release there."""
    user = system.invoking_user
    if user.user_id == ROOT_USER_ID:
        raise ReleaseSetupError(
            "steamshare is set up for the desktop user who runs Steam, and root is not that "
            "user. Run the one-line install with sudo from the desktop user account."
        )
    folder = choose_install_folder(user, system.system_folder, is_read_only=system.is_read_only)
    if DRY_RUN_FLAG in install_arguments:
        print(
            f"Dry run: steamshare {version} would be placed in {folder.path}. The plan below "
            "runs from the downloaded copy, so it names a temporary folder where a real run "
            f"names {folder.path}.",
            flush=True,
        )
        return source / LAUNCHER_FILE_NAME
    previous = installed_release_version(folder.path)
    _place(source, version, folder, change_owner=system.change_owner)
    replaced = "" if previous is None else f" (it replaced {previous})"
    # Flushed: the launcher replaces this process, and unflushed output is lost.
    print(f"steamshare {version} is in {folder.path}{replaced}.", flush=True)
    return folder.path / LAUNCHER_FILE_NAME


def _place(source: Path, version: str, folder: InstallFolder, *, change_owner: ChangeOwner) -> None:
    owner = folder.owner
    try:
        if owner is not None:
            # ~/.local/share may not exist yet; root would otherwise own it.
            create_owned_directories(
                [folder.path.parent], owner.user_id, owner.group_id, change_owner=change_owner
            )
        place_release(
            source,
            folder.path,
            version=version,
            owner=None if owner is None else (owner.user_id, owner.group_id),
            change_owner=change_owner,
        )
    except ForeignFolderError as error:
        raise ReleaseSetupError(
            f"{error.path} exists and is not a steamshare release install (it has no "
            f"{RELEASE_MARKER_FILE_NAME} file), so it was left alone. If it is a clone of "
            f"steamshare, run `{SUDO_INSTALL_COMMAND}` from it instead; otherwise move it "
            "away and run the one-line install again."
        ) from error
    except OSError as error:
        raise ReleaseSetupError(
            f"Could not place steamshare in {folder.path}: {error.strerror} "
            f"({error.filename}). Fix the cause and run the one-line install again."
        ) from error


def _start_launcher(launcher: Path, arguments: list[str]) -> None:
    os.execv(launcher, [str(launcher), *arguments])


def main(argv: list[str] | None = None) -> int:
    # No argparse: every argument after the first two belongs to install,
    # options included, and install parses them itself.
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 2:
        print(
            "Usage: python3 -m steamshare.release_setup <release folder> <version> "
            "[<install options>]. release/install.sh runs this; see the README to install.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    source, version, *install_arguments = arguments
    system = System(
        invoking_user=resolve_invoking_user(),
        system_folder=SYSTEM_INSTALL_FOLDER,
        is_read_only=is_on_read_only_filesystem,
        change_owner=os.lchown,
        start_launcher=_start_launcher,
    )
    return release_setup(Path(source), version, install_arguments, system)


if __name__ == "__main__":
    sys.exit(main())
