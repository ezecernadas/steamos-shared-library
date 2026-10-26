"""Integration tests on a real NTFS filesystem, mounted the way `install` does it.

Why these exist: the unit tests fake the mount table and the disk. These
build a small NTFS image, mount it with `ntfs3` and `windows_names`,
bind-mount the Linux-only folders from the host filesystem, and check the
two promises the project rests on:

- Windows-invalid names cannot reach NTFS, but still work in the bind mounts;
- `doctor` sees a correct setup as correct, and a broken one as broken.

They need root (to mount) plus `mkntfs` and the `ntfs3` module, so they run
in CI (`.github/workflows/ci.yml`) and are skipped anywhere else:

    sudo .venv/bin/python -m pytest tests/integration
"""

import errno
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from steamshare.cli.doctor import exit_code_for, run_checks
from steamshare.core.doctor import Status
from steamshare.core.library_layout import LINUX_ONLY_DIRECTORIES, STEAMAPPS_DIRECTORY

# Big enough for mkntfs and a few files; the content stays tiny.
IMAGE_SIZE = "64M"
LIBRARY_FOLDER = "SteamLibrary"
# The options `install` writes into the mount unit (core.mounts), minus
# `nofail`, which only matters at boot.
NTFS3_OPTIONS = "uid=0,gid=0,umask=022,windows_names"

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or shutil.which("mkntfs") is None,
    reason="needs root and mkntfs (ntfs-3g); runs in CI",
)


@dataclass(frozen=True)
class MountedLibrary:
    library: Path
    steamapps: Path
    # The host folders that hold the real Linux-only data.
    linux_only_sources: dict[str, Path]


def run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mounted_library(tmp_path: Path) -> Iterator[MountedLibrary]:
    """An NTFS image mounted like the real setup, unmounted again afterwards."""
    image = tmp_path / "library.img"
    mount_point = tmp_path / "steamlib"
    mount_point.mkdir()
    run("truncate", "--size", IMAGE_SIZE, str(image))
    loop_device = run("losetup", "--find", "--show", str(image))
    # Teardown undoes, in reverse order, whatever was set up before a failure.
    undo: list[tuple[str, ...]] = [("losetup", "--detach", loop_device)]
    try:
        run("mkntfs", "--quick", "--force", "--quiet", loop_device)
        run("mount", "-t", "ntfs3", "-o", NTFS3_OPTIONS, loop_device, str(mount_point))
        undo.append(("umount", str(mount_point)))

        steamapps = mount_point / LIBRARY_FOLDER / STEAMAPPS_DIRECTORY
        sources = {}
        for name in LINUX_ONLY_DIRECTORIES:
            source = tmp_path / "steam-shared" / name
            source.mkdir(parents=True)
            (steamapps / name).mkdir(parents=True)
            run("mount", "--bind", str(source), str(steamapps / name))
            undo.append(("umount", str(steamapps / name)))
            sources[name] = source

        yield MountedLibrary(mount_point / LIBRARY_FOLDER, steamapps, sources)
    finally:
        for command in reversed(undo):
            subprocess.run(command, check=False, capture_output=True)


# --- windows_names ---------------------------------------------------------------


def test_ntfs_rejects_the_name_proton_creates(mounted_library: MountedLibrary) -> None:
    with pytest.raises(OSError) as raised:
        (mounted_library.steamapps / "c:").touch()

    assert raised.value.errno == errno.EINVAL


def test_ntfs_accepts_ordinary_names(mounted_library: MountedLibrary) -> None:
    game_file = mounted_library.steamapps / "common" / "Game" / "game.exe"
    game_file.parent.mkdir(parents=True)
    game_file.touch()

    assert game_file.exists()


def test_compatdata_accepts_the_name_because_it_is_not_ntfs(
    mounted_library: MountedLibrary,
) -> None:
    drive_link = mounted_library.steamapps / "compatdata" / "1005870" / "pfx" / "dosdevices" / "c:"
    drive_link.parent.mkdir(parents=True)
    drive_link.symlink_to("../drive_c")

    # The name landed on the host filesystem, through the bind mount.
    assert (
        mounted_library.linux_only_sources["compatdata"]
        / drive_link.relative_to(mounted_library.steamapps / "compatdata")
    ).is_symlink()


# --- doctor ------------------------------------------------------------------------


def test_doctor_passes_on_a_correct_setup(mounted_library: MountedLibrary) -> None:
    # Proton's names inside the bind mount must not count as NTFS problems.
    prefix = mounted_library.steamapps / "compatdata" / "48000" / "pfx" / "dosdevices"
    prefix.mkdir(parents=True)
    (prefix / "c:").symlink_to("../drive_c")

    results = run_checks(mounted_library.library)

    assert [result.status for result in results] == [Status.OK] * len(results)
    assert exit_code_for(results) == 0


def test_doctor_fails_when_a_bind_mount_is_missing(mounted_library: MountedLibrary) -> None:
    run("umount", str(mounted_library.steamapps / "shadercache"))

    results = run_checks(mounted_library.library)

    failed_titles = [result.title for result in results if result.status is Status.FAILURE]
    assert failed_titles == ["compatdata and shadercache are bind-mounted from ext4"]
    assert exit_code_for(results) == 1


def test_doctor_reports_a_symlink_on_ntfs(mounted_library: MountedLibrary) -> None:
    # What Linux Steam does with Steam.dll (docs/PLAN.md, "Phase 0 results").
    common = mounted_library.steamapps / "common"
    common.mkdir()
    (common / "Steam.dll").symlink_to("/home/user/.steam/legacycompat/Steam.dll")

    results = run_checks(mounted_library.library)

    warnings = [result for result in results if result.status is Status.WARNING]
    assert len(warnings) == 1
    assert warnings[0].details == ("steamapps/common/Steam.dll",)
