"""Look inside an NTFS partition, read-only, to tell what it holds.

`install` shows what is on each NTFS partition so the user can pick the one
with the library. A partition that is already mounted is read where it is.
Any other one is mounted read-only in a fresh temporary folder, read, and
unmounted again, which needs root.

Why a read-only mount is safe, even on a volume Windows left hibernated (Fast
Startup) or dirty: nothing is written, not even the "volume mounted" flag or
access times, so Windows finds the volume exactly as it left it. If ntfs3
still refuses the volume, the partition is reported as "could not inspect",
with the kernel's reason.

The commands and the temporary folder are parameters, so tests run the
inspection on plain folder trees without root.
"""

import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from steamshare.core.library_layout import STEAMAPPS_DIRECTORY
from steamshare.core.mounts import NTFS3_FILESYSTEM, READ_ONLY_OPTION
from steamshare.core.ntfs_partitions import (
    RECOVERY_FOLDER,
    WINDOWS_SYSTEM_MARKER,
    NtfsPartition,
    PartitionContents,
    PartitionFacts,
    could_not_inspect,
    not_inspected_without_root,
)
from steamshare.core.systemd_units import MODPROBE_PATH, NTFS3_KERNEL_MODULE

MOUNT_COMMAND = "mount"
UNMOUNT_COMMAND = "umount"

# Makes the temporary mount easy to recognize in `findmnt` if it is ever left
# behind.
PROBE_DIRECTORY_PREFIX = "steamshare-probe-"

# Runs one command and raises `subprocess.CalledProcessError` if it fails.
CommandRunner = Callable[[Sequence[str]], None]


class PartitionCleanupError(Exception):
    """The temporary mount could not be undone; the message says how to finish."""


def run_capturing_errors(command: Sequence[str]) -> None:
    """Run a command, keeping its error output for the failure's reason."""
    subprocess.run(command, check=True, capture_output=True, text=True)


def make_probe_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix=PROBE_DIRECTORY_PREFIX))


# --- What is inside ----------------------------------------------------------


def inspect_contents(root: Path) -> PartitionContents:
    """Return what the partition mounted at `root` holds.

    Raises OSError when `root` itself cannot be read: then nothing is known.
    """
    return PartitionContents(
        has_windows_system=os.path.isdir(root / WINDOWS_SYSTEM_MARKER),
        has_recovery_folder=os.path.isdir(root / RECOVERY_FOLDER),
        library_folders=find_library_folders(root),
    )


def find_library_folders(root: Path) -> tuple[str, ...]:
    """Return the folders, one or two levels down, that hold a Steam library.

    Steam's "Add Drive" creates its library one level down (`D:\\SteamLibrary`);
    Steam's own install keeps one two levels down (`Program Files (x86)\\Steam`).
    Looking no deeper keeps the walk to a few thousand folders: deeper levels
    are the games themselves.

    A folder holds a library when it contains `steamapps`, where Steam keeps
    the games. `libraryfolder.vdf` is not required, so a library is still
    found if that small file is missing.
    """
    top_folders = _subfolders(root)
    second_level = [child for folder in top_folders for child in _readable_subfolders(folder)]
    return tuple(
        sorted(
            folder.relative_to(root).as_posix()
            for folder in [*top_folders, *second_level]
            if _is_real_folder(folder / STEAMAPPS_DIRECTORY)
        )
    )


def _subfolders(folder: Path) -> list[Path]:
    return [entry for entry in folder.iterdir() if _is_real_folder(entry)]


def _readable_subfolders(folder: Path) -> list[Path]:
    # Windows locks some folders, such as "System Volume Information".
    try:
        return _subfolders(folder)
    except OSError:
        return []


def _is_real_folder(path: Path) -> bool:
    # Symlinks are not followed: on NTFS they can point anywhere, even back up
    # the tree. os.path.isdir, unlike Path.is_dir, treats "permission denied"
    # as "no folder" instead of raising.
    return os.path.isdir(path) and not os.path.islink(path)


# --- Probing -----------------------------------------------------------------


def probe_partition(
    partition: NtfsPartition,
    *,
    can_mount: bool,
    run: CommandRunner = run_capturing_errors,
    make_directory: Callable[[], Path] = make_probe_directory,
    remove_directory: Callable[[Path], None] = os.rmdir,
) -> PartitionFacts:
    """Return what `partition` holds, mounting it read-only if needed.

    `can_mount` is False without root: a partition that is not mounted then
    stays unknown.
    """
    if partition.mount_point is not None:
        return _inspect_at(partition, Path(partition.mount_point))
    if not can_mount:
        return not_inspected_without_root(partition)
    try:
        # Explicitly, because some distros blacklist ntfs3 and the kernel's
        # own autoload honors that (see `core.systemd_units`).
        run([str(MODPROBE_PATH), NTFS3_KERNEL_MODULE])
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        return could_not_inspect(
            partition, f"could not load the {NTFS3_KERNEL_MODULE} module ({_reason(error)})"
        )
    return _inspect_with_temporary_mount(partition, run, make_directory(), remove_directory)


def _inspect_with_temporary_mount(
    partition: NtfsPartition,
    run: CommandRunner,
    directory: Path,
    remove_directory: Callable[[Path], None],
) -> PartitionFacts:
    mount = [
        MOUNT_COMMAND,
        "-t",
        NTFS3_FILESYSTEM,
        "-o",
        READ_ONLY_OPTION,
        str(partition.device),
        str(directory),
    ]
    try:
        run(mount)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        remove_directory(directory)
        return could_not_inspect(partition, _reason(error))
    try:
        return _inspect_at(partition, directory)
    finally:
        _unmount(partition, directory, run)
        # Only after a successful unmount: removing the folder of a live
        # mount fails, and the folder is the user's handle to clean up.
        remove_directory(directory)


def _inspect_at(partition: NtfsPartition, root: Path) -> PartitionFacts:
    try:
        return PartitionFacts(partition=partition, contents=inspect_contents(root))
    except OSError as error:
        return could_not_inspect(partition, error.strerror or str(error))


def _unmount(partition: NtfsPartition, directory: Path, run: CommandRunner) -> None:
    try:
        run([UNMOUNT_COMMAND, str(directory)])
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise PartitionCleanupError(
            f"{partition.device} is still mounted read-only at {directory} ({_reason(error)}). "
            f"Close anything using it, then run `sudo umount {directory}` "
            f"and `sudo rmdir {directory}`."
        ) from error


def _reason(error: subprocess.CalledProcessError | FileNotFoundError) -> str:
    if isinstance(error, FileNotFoundError):
        return f"{error.filename} not found"
    stderr = (error.stderr or "").strip()
    return stderr or f"exit status {error.returncode}"
