"""The `doctor` checks: one pure function per check, each returning a `CheckResult`.

Why this matters: Windows and Linux share one library only while a few
conditions hold. The partition must use the `ntfs3` driver with
`windows_names`, the Linux-only folders must be bind-mounted from ext4, and no
Windows-invalid name may reach NTFS. Each check below verifies one of them and
tells the user how to fix it.

The checks take plain data that the adapters gathered (mounts, library
entries), so they do no I/O. To add a check, write a new function here and
call it from the CLI.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from steamshare.core.doctor import CheckResult, Status
from steamshare.core.library_layout import (
    LINUX_ONLY_DATA_DIRECTORY,
    LINUX_ONLY_DIRECTORIES,
    LibraryEntry,
    is_inside_linux_tool,
)
from steamshare.core.mounts import (
    NTFS3_FILESYSTEM,
    NTFS3_MOUNT_OPTIONS,
    NTFS_3G_FILESYSTEM,
    READ_ONLY_OPTION,
    WINDOWS_NAMES_OPTION,
    MountPoint,
    find_mount_at,
)
from steamshare.core.windows_names import find_name_violation

# A library can hold thousands of problems; past this many, listing more
# only buries the first ones.
MAX_LISTED_PATHS = 20

MOUNT_COMMAND = f"sudo mount -t ntfs3 -o {NTFS3_MOUNT_OPTIONS} <partition> <mount point>"

# Filesystems that put a folder on the NTFS partition, whichever driver is used.
NTFS_FILESYSTEMS = frozenset({NTFS3_FILESYSTEM, NTFS_3G_FILESYSTEM})


# --- Library mount ---------------------------------------------------------


@dataclass(frozen=True)
class MountProblem:
    """Why the library mount is wrong, and how to fix it."""

    detail: str
    hint: str


# A rule returns the problem when the mount breaks it, or None.
MountRule = Callable[[MountPoint], MountProblem | None]


def uses_ntfs_3g_driver(mount: MountPoint) -> MountProblem | None:
    if mount.filesystem_type != NTFS_3G_FILESYSTEM:
        return None
    return MountProblem(
        detail="Mounted with the ntfs-3g FUSE driver (fuseblk), which has no windows_names.",
        hint=(
            "Some distros blacklist ntfs3. Run `sudo modprobe ntfs3`, unmount the "
            f"partition, and mount it again with: {MOUNT_COMMAND}"
        ),
    )


def uses_other_filesystem(mount: MountPoint) -> MountProblem | None:
    # When the partition is not mounted, the library path falls on whatever
    # holds the mount point (usually the root ext4), so this is also the
    # typical "not mounted" case.
    if mount.filesystem_type in NTFS_FILESYSTEMS:
        return None
    return MountProblem(
        detail=f"{mount.target} is {mount.filesystem_type}, not ntfs3.",
        hint=f"Mount the NTFS partition with: {MOUNT_COMMAND}",
    )


def is_read_only(mount: MountPoint) -> MountProblem | None:
    if READ_ONLY_OPTION not in mount.options:
        return None
    return MountProblem(
        detail="Mounted read-only, usually because Windows left the volume dirty.",
        hint=(
            "Windows Fast Startup or hibernation left the volume dirty. Boot Windows, "
            "run `powercfg /h off` as administrator, and shut down fully."
        ),
    )


def lacks_windows_names(mount: MountPoint) -> MountProblem | None:
    if WINDOWS_NAMES_OPTION in mount.options:
        return None
    return MountProblem(
        detail=(
            "Mounted without windows_names, so Linux can write names (such as `c:`) "
            "that Windows treats as corruption."
        ),
        hint=f"Unmount the partition and mount it again with: {MOUNT_COMMAND}",
    )


# Ordered so the first problem reported is the one to fix first: a wrong
# driver makes the option checks meaningless.
MOUNT_RULES: tuple[MountRule, ...] = (
    uses_ntfs_3g_driver,
    uses_other_filesystem,
    is_read_only,
    lacks_windows_names,
)

LIBRARY_MOUNT_TITLE = "Library partition is mounted with ntfs3 and windows_names"


def check_library_mount(mount: MountPoint | None) -> CheckResult:
    """Check the mount that holds the library folder."""
    if mount is None:
        return CheckResult(
            title=LIBRARY_MOUNT_TITLE,
            status=Status.FAILURE,
            details=("No mount holds the library folder.",),
            hint=f"Mount the NTFS partition with: {MOUNT_COMMAND}",
        )
    for rule in MOUNT_RULES:
        problem = rule(mount)
        if problem is not None:
            return CheckResult(
                title=LIBRARY_MOUNT_TITLE,
                status=Status.FAILURE,
                details=(problem.detail,),
                hint=problem.hint,
            )
    return CheckResult(
        title=LIBRARY_MOUNT_TITLE,
        status=Status.OK,
        details=(f"{mount.source} on {mount.target}",),
    )


# --- Linux-only folders ----------------------------------------------------


def check_linux_only_directories(
    mounts: Iterable[MountPoint], steamapps: PurePosixPath
) -> CheckResult:
    """Check that each Linux-only folder is bind-mounted from outside NTFS."""
    mounts = list(mounts)
    problems: list[str] = []
    bind_commands: list[str] = []
    for name in LINUX_ONLY_DIRECTORIES:
        target = steamapps / name
        problem = _find_linux_only_directory_problem(find_mount_at(mounts, target), target)
        if problem is not None:
            problems.append(problem)
            bind_commands.append(f"sudo mount --bind ~/{LINUX_ONLY_DATA_DIRECTORY / name} {target}")

    title = f"{' and '.join(LINUX_ONLY_DIRECTORIES)} are bind-mounted from ext4"
    if not problems:
        return CheckResult(title=title, status=Status.OK)
    return CheckResult(
        title=title,
        status=Status.FAILURE,
        details=tuple(problems),
        hint="Bind-mount them from ext4:\n" + "\n".join(bind_commands),
    )


def _find_linux_only_directory_problem(
    mount: MountPoint | None, target: PurePosixPath
) -> str | None:
    if mount is None:
        return f"{target} is not a mount point, so its files land on NTFS."
    if mount.filesystem_type in NTFS_FILESYSTEMS:
        return f"{target} is mounted from NTFS ({mount.filesystem_type}), not from ext4."
    return None


# --- Library contents ------------------------------------------------------


def check_library_folder(library: PurePosixPath, is_directory: bool) -> CheckResult:
    """Check that the library folder exists, since the content checks walk it."""
    title = "Library folder exists"
    if is_directory:
        return CheckResult(title=title, status=Status.OK, details=(str(library),))
    return CheckResult(
        title=title,
        status=Status.FAILURE,
        details=(f"{library} is not a folder, so its contents were not checked.",),
        hint=(
            "Mount the NTFS partition first, then pass the folder that contains "
            "`steamapps` with --library."
        ),
    )


def check_windows_names(entries: Iterable[LibraryEntry]) -> CheckResult:
    """List library entries whose name Windows would reject.

    Only each entry's own name is checked, not its parent folders: the walker
    yields every folder as an entry too, so a bad folder is reported once
    instead of once for every file inside it.
    """
    offending = []
    for entry in entries:
        violation = find_name_violation(entry.relative_path.name)
        if violation is not None:
            offending.append(f"{entry.relative_path} ({violation.reason})")

    title = "No Windows-invalid names on NTFS"
    if not offending:
        return CheckResult(title=title, status=Status.OK)
    return CheckResult(
        title=title,
        status=Status.FAILURE,
        details=_cap_listed_paths(offending),
        hint=(
            "Rename or remove these from Linux before booting Windows; Windows and "
            "chkdsk treat them as corruption. Then mount with windows_names so it "
            "cannot happen again."
        ),
    )


def check_unexpected_symlinks(entries: Iterable[LibraryEntry]) -> CheckResult:
    """List symlinks on NTFS outside the Linux tool folders.

    Windows cannot follow symlinks to Linux paths. Linux Steam creates
    `steamapps/common/Steam.dll` as a symlink to a Linux path, so that one is
    expected to show up here; Windows Steam has ignored it so far.
    """
    unexpected = [
        str(entry.relative_path)
        for entry in entries
        if entry.is_symlink and not is_inside_linux_tool(entry)
    ]

    title = "No unexpected symlinks on NTFS"
    if not unexpected:
        return CheckResult(title=title, status=Status.OK)
    return CheckResult(
        title=title,
        status=Status.WARNING,
        details=_cap_listed_paths(unexpected),
        hint="Windows cannot follow these. Check that Windows Steam still works with them.",
    )


def _cap_listed_paths(lines: list[str]) -> tuple[str, ...]:
    if len(lines) <= MAX_LISTED_PATHS:
        return tuple(lines)
    hidden_count = len(lines) - MAX_LISTED_PATHS
    return (*lines[:MAX_LISTED_PATHS], f"...and {hidden_count} more")
