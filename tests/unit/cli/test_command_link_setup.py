"""Tests for the `steamshare` command link that `install` puts on PATH.

The system command folder and the user's home are temporary folders, the
read-only check is a flag, and owner changes are recorded instead of made.
"""

import os
from pathlib import Path, PurePosixPath

import pytest

from steamshare.adapters.invoking_user import InvokingUser
from steamshare.cli.command_link_setup import (
    CommandLinkPlan,
    CommandLinkSkipped,
    apply_command_link,
    command_link_summary_field,
    plan_command_link,
    preview_command_link,
)
from steamshare.cli.install_choices import InstallError
from steamshare.cli.run_report import UNICODE_SYMBOLS, ClosingNotes, RunReport


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.system_directory = tmp_path / "usr-local-bin"
        self.system_directory.mkdir()
        self.system_is_read_only = False
        repository = tmp_path / "repository"
        (repository / "src" / "steamshare").mkdir(parents=True)
        self.launcher = repository / "steamshare"
        self.launcher.write_text("#!/usr/bin/env bash\n")
        self.user = InvokingUser(1000, 1000, "player", PurePosixPath(self.home))
        self.owned: list[Path] = []
        self.links_owned: list[Path] = []

    @property
    def system_link(self) -> Path:
        return self.system_directory / "steamshare"

    @property
    def user_link(self) -> Path:
        return self.home / ".local" / "bin" / "steamshare"

    def plan(self) -> CommandLinkPlan | CommandLinkSkipped:
        return plan_command_link(
            user=self.user,
            launcher=self.launcher,
            system_directory=self.system_directory,
            is_read_only=lambda directory: self.system_is_read_only,
        )

    def report(self, *, dry_run: bool) -> RunReport:
        return RunReport(symbols=UNICODE_SYMBOLS, home=self.user.home, dry_run=dry_run)

    def apply(self) -> ClosingNotes:
        return apply_command_link(
            self.plan(),
            change_owner=lambda path, user_id, group_id: self.owned.append(path),
            change_link_owner=lambda path, user_id, group_id: self.links_owned.append(path),
            report=self.report(dry_run=False),
        )

    def preview(self) -> ClosingNotes:
        return preview_command_link(self.plan(), self.report(dry_run=True))


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


# --- Where the link goes ---------------------------------------------------------


def test_links_the_launcher_in_the_system_command_folder(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = setup.apply()

    assert os.readlink(setup.system_link) == str(setup.launcher)
    assert setup.owned == [] and setup.links_owned == []
    assert notes.next_steps == ()
    assert "✓ created" in capsys.readouterr().out


def test_on_a_read_only_root_links_in_the_users_own_folder_given_to_the_user(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.system_is_read_only = True

    notes = setup.apply()

    assert os.readlink(setup.user_link) == str(setup.launcher)
    assert not setup.system_link.exists()
    assert setup.owned == [setup.home / ".local", setup.user_link.parent]
    assert setup.links_owned == [setup.user_link]
    # Root cannot see the user's login PATH, so the step says what to do if needed.
    [step] = notes.next_steps
    assert "~/.local/bin" in step and "PATH" in step


def test_without_a_system_command_folder_links_in_the_users_folder(setup: Setup) -> None:
    setup.system_directory.rmdir()

    setup.apply()

    assert setup.user_link.is_symlink()


def test_the_summary_names_where_the_command_goes(setup: Setup) -> None:
    label, value = command_link_summary_field(setup.plan())

    assert label == "Command"
    assert str(setup.system_link) in value


# --- What is there already -------------------------------------------------------


def test_a_second_run_leaves_the_link_unchanged(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.apply()
    capsys.readouterr()

    setup.apply()

    assert "✓ unchanged" in capsys.readouterr().out


def test_a_link_to_a_moved_checkout_is_updated(
    tmp_path: Path, setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "old-clone"
    (old / "src" / "steamshare").mkdir(parents=True)
    (old / "steamshare").write_text("")
    setup.system_link.symlink_to(old / "steamshare")

    setup.apply()

    assert os.readlink(setup.system_link) == str(setup.launcher)
    assert "✓ updated" in capsys.readouterr().out


def test_another_file_with_the_name_is_never_replaced(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.system_link.write_text("#!/bin/sh\necho mine\n")

    notes = setup.apply()

    assert setup.system_link.read_text() == "#!/bin/sh\necho mine\n"
    assert "skipped" in capsys.readouterr().out
    [step] = notes.next_steps
    assert str(setup.system_link) in step and "./steamshare" in step


# --- Dry run, and failures -----------------------------------------------------


def test_a_dry_run_shows_the_link_and_creates_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.system_is_read_only = True

    notes = setup.preview()

    assert not setup.user_link.parent.exists()
    output = capsys.readouterr().out
    assert "would create" in output and "steamshare" in output
    assert notes.next_steps  # the PATH step, as after a real run


def test_without_the_launcher_the_link_is_skipped(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.launcher.unlink()

    assert isinstance(setup.plan(), CommandLinkSkipped)
    setup.apply()
    assert "skipped" in capsys.readouterr().out


def test_a_failed_link_says_what_is_already_done_and_what_to_do(setup: Setup) -> None:
    setup.system_directory.chmod(0o500)
    try:
        if os.access(setup.system_directory, os.W_OK):
            pytest.skip("running as root, which can write anyway")
        with pytest.raises(InstallError, match="./steamshare"):
            setup.apply()
    finally:
        setup.system_directory.chmod(0o700)
