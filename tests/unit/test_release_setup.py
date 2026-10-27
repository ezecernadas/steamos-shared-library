"""Tests for the step between the one-line install's download and `install`.

The machine is replaced by a `System` of temporary folders and recorders, so
nothing runs as root and no launcher is started.
"""

from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.invoking_user import InvokingUser
from steamshare.adapters.release_install import installed_release_version
from steamshare.release_setup import (
    InstallFolder,
    System,
    choose_install_folder,
    main,
    release_setup,
)

PLAYER = InvokingUser(1000, 1000, "player", PurePosixPath("/home/player"))


class FakeMachine:
    def __init__(self, tmp_path: Path) -> None:
        self.source = tmp_path / "download" / "steamshare-0.2.0"
        (self.source / "src" / "steamshare").mkdir(parents=True)
        (self.source / "steamshare").write_text("#!/usr/bin/env bash\n")
        self.home = tmp_path / "home" / "player"
        self.home.mkdir(parents=True)
        self.opt = tmp_path / "opt"
        self.opt.mkdir()
        self.user = InvokingUser(1000, 1000, "player", PurePosixPath(self.home))
        self.read_only: set[Path] = set()
        self.owned: list[Path] = []
        self.started: list[tuple[Path, list[str]]] = []

    def build(self) -> System:
        return System(
            invoking_user=self.user,
            system_folder=self.opt / "steamshare",
            is_read_only=lambda folder: folder in self.read_only,
            change_owner=lambda path, user_id, group_id: self.owned.append(path),
            start_launcher=self._start,
        )

    def _start(self, launcher: Path, arguments: list[str]) -> None:
        # The real one replaces the process; this one records and returns.
        self.started.append((launcher, arguments))


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


def run(machine: FakeMachine, *install_arguments: str) -> int:
    return release_setup(machine.source, "0.2.0", list(install_arguments), machine.build())


# --- Where it goes --------------------------------------------------------------


def test_the_system_folder_is_chosen_where_it_can_be_written(tmp_path: Path) -> None:
    system_folder = tmp_path / "opt" / "steamshare"
    system_folder.parent.mkdir()

    folder = choose_install_folder(PLAYER, system_folder, is_read_only=lambda _: False)

    assert folder == InstallFolder(path=system_folder, owner=None)


def test_the_users_folder_is_chosen_where_the_system_one_is_read_only(tmp_path: Path) -> None:
    system_folder = tmp_path / "opt" / "steamshare"
    system_folder.parent.mkdir()

    folder = choose_install_folder(PLAYER, system_folder, is_read_only=lambda _: True)

    assert folder == InstallFolder(path=Path("/home/player/.local/share/steamshare"), owner=PLAYER)


def test_the_users_folder_is_chosen_when_there_is_no_system_folder(tmp_path: Path) -> None:
    folder = choose_install_folder(
        PLAYER, tmp_path / "missing" / "steamshare", is_read_only=lambda _: False
    )

    assert folder.owner == PLAYER


# --- A real run -----------------------------------------------------------------


def test_the_release_is_placed_and_install_runs_from_it(machine: FakeMachine) -> None:
    code = run(machine, "--yes", "--partition-uuid", "1111AAAA2222BBBB")

    destination = machine.opt / "steamshare"
    assert code == 0
    assert installed_release_version(destination) == "0.2.0"
    assert machine.started == [
        (destination / "steamshare", ["install", "--yes", "--partition-uuid", "1111AAAA2222BBBB"])
    ]


def test_it_says_where_the_release_went_and_what_it_replaced(
    machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    run(machine)
    capsys.readouterr()

    run(machine)

    output = capsys.readouterr().out
    assert f"steamshare 0.2.0 is in {machine.opt / 'steamshare'}" in output
    assert "(it replaced 0.2.0)" in output


def test_on_a_read_only_root_the_release_goes_to_the_users_home(machine: FakeMachine) -> None:
    machine.read_only.add(machine.opt)

    run(machine)

    destination = machine.home / ".local" / "share" / "steamshare"
    assert installed_release_version(destination) == "0.2.0"
    assert machine.started[0][0] == destination / "steamshare"
    # The missing folders on the way, and every file, are the user's.
    assert machine.home / ".local" in machine.owned
    assert any(path.name == "src" for path in machine.owned)


def test_a_folder_that_is_not_a_release_is_left_alone(
    machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = machine.opt / "steamshare"
    (clone / "src").mkdir(parents=True)

    code = run(machine)

    assert code == 1
    error = capsys.readouterr().err
    assert f"{clone} exists and is not a steamshare release install" in error
    assert "sudo ./steamshare install" in error
    assert machine.started == []


def test_root_without_sudo_is_refused_before_anything_is_placed(
    machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.user = InvokingUser(0, 0, "root", PurePosixPath("/root"))

    code = run(machine)

    assert code == 1
    assert "with sudo from the desktop user account" in capsys.readouterr().err
    assert not (machine.opt / "steamshare").exists()
    assert machine.started == []


# --- A dry run ------------------------------------------------------------------


def test_a_dry_run_places_nothing_and_runs_install_from_the_download(
    machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(machine, "--dry-run")

    assert code == 0
    assert not (machine.opt / "steamshare").exists()
    assert machine.started == [(machine.source / "steamshare", ["install", "--dry-run"])]
    output = capsys.readouterr().out
    assert f"would be placed in {machine.opt / 'steamshare'}" in output
    assert "temporary folder" in output


def test_main_without_the_folder_and_version_says_how_it_is_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    assert "release/install.sh runs this" in capsys.readouterr().err
