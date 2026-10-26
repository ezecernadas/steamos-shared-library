"""Tests for the `install` command's wiring, with every system effect faked.

The unit directory is a temporary folder, and root, the partition lookup,
systemctl, and chown are replaced, so the tests run without root and change
nothing on the machine.
"""

import argparse
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.invoking_user import InvokingUser
from steamshare.cli.install import (
    EXIT_FAILURE,
    EXIT_OK,
    System,
    absolute_path,
    folder_name,
    install,
)

USER = InvokingUser(user_id=1000, group_id=1000, home=PurePosixPath("/home/ezecer"))
EXPECTED_UNITS = [
    "steamshare-ntfs3.service",
    "mnt-steamlib.mount",
    "mnt-steamlib-SteamLibrary-steamapps-compatdata.mount",
    "mnt-steamlib-SteamLibrary-steamapps-shadercache.mount",
]


class FakeSystem:
    """Builds a `System` whose effects are recorded instead of applied."""

    def __init__(self, tmp_path: Path, *, is_root: bool = True, user: InvokingUser = USER) -> None:
        self.unit_directory = tmp_path / "units"
        self.unit_directory.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.enabled: list[list[str]] = []
        self.owners: list[Path] = []
        self.partition_present = True
        self.enable_error: Exception | None = None
        self.is_root = is_root
        self.user = InvokingUser(user.user_id, user.group_id, PurePosixPath(self.home))

    def build(self) -> System:
        return System(
            is_root=self.is_root,
            invoking_user=self.user,
            unit_directory=self.unit_directory,
            partition_exists=lambda device: self.partition_present,
            enable_units=self._enable,
            change_owner=lambda path, user_id, group_id: self.owners.append(path),
        )

    def _enable(self, unit_names: list[str]) -> None:
        if self.enable_error is not None:
            raise self.enable_error
        self.enabled.append(unit_names)


def make_arguments(dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        partition_uuid="D8E665D5E665B502",
        mount_point=PurePosixPath("/mnt/steamlib"),
        library_folder="SteamLibrary",
        dry_run=dry_run,
    )


def test_real_run_writes_units_creates_folders_and_enables_the_mounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_OK
    assert sorted(path.name for path in system.unit_directory.iterdir()) == sorted(EXPECTED_UNITS)
    assert (system.home / ".local/share/steam-shared/compatdata").is_dir()
    assert system.enabled == [EXPECTED_UNITS[1:]]
    output = capsys.readouterr().out
    assert "created" in output
    assert "steamshare doctor --library /mnt/steamlib/SteamLibrary" in output


def test_second_run_reports_every_unit_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    install(make_arguments(), system.build())
    capsys.readouterr()

    install(make_arguments(), system.build())

    output = capsys.readouterr().out
    assert output.count("unchanged") == len(EXPECTED_UNITS) + 2  # plus the two folders
    assert "created" not in output


def test_dry_run_prints_every_unit_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    exit_code = install(make_arguments(dry_run=True), system.build())

    assert exit_code == EXIT_OK
    assert list(system.unit_directory.iterdir()) == []
    assert not (system.home / ".local").exists()
    assert system.enabled == []
    output = capsys.readouterr().out
    for name in EXPECTED_UNITS:
        assert name in output
    assert "Options=uid=1000,gid=1000,umask=022,windows_names,nofail" in output
    assert "systemctl enable --now mnt-steamlib.mount" in output


def test_refuses_to_run_without_root_and_explains_how(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path, is_root=False)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert list(system.unit_directory.iterdir()) == []
    error = capsys.readouterr().err
    assert "sudo .venv/bin/python -m steamshare install --partition-uuid D8E665D5E665B502" in error
    assert "--dry-run" in error


def test_refuses_to_install_for_root_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = InvokingUser(user_id=0, group_id=0, home=PurePosixPath("/root"))
    system = FakeSystem(tmp_path, user=root)

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert "desktop user" in capsys.readouterr().err


def test_refuses_an_unknown_partition_uuid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.partition_present = False

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    assert list(system.unit_directory.iterdir()) == []
    error = capsys.readouterr().err
    assert "/dev/disk/by-uuid/D8E665D5E665B502" in error
    assert "lsblk -f" in error


def test_reports_a_systemctl_failure_with_the_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(tmp_path)
    system.enable_error = subprocess.CalledProcessError(1, ["systemctl", "enable"])

    exit_code = install(make_arguments(), system.build())

    assert exit_code == EXIT_FAILURE
    error = capsys.readouterr().err
    assert "systemctl status mnt-steamlib.mount" in error


# --- Argument validation -----------------------------------------------------


@pytest.mark.parametrize("value", ["mnt/steamlib", "/", ""])
def test_mount_point_must_be_an_absolute_folder_below_root(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        absolute_path(value)


def test_mount_point_accepts_an_absolute_folder() -> None:
    assert absolute_path("/mnt/steamlib") == PurePosixPath("/mnt/steamlib")


@pytest.mark.parametrize("value", ["", "a/b", ".", ".."])
def test_library_folder_must_be_a_single_folder_name(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        folder_name(value)
