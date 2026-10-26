"""Tests for detecting a running Steam client from a fake /proc.

The detection must not mistake processes that merely mention "steam" for the
client, such as this very `python -m steamshare` process.
"""

from pathlib import Path

from steamshare.adapters.steam_process import is_steam_running


def add_process(proc: Path, pid: int, name: str, executable: Path | None = None) -> None:
    folder = proc / str(pid)
    folder.mkdir(parents=True)
    (folder / "comm").write_text(f"{name}\n")
    if executable is not None:
        (folder / "exe").symlink_to(executable)


def make_steam_root(tmp_path: Path) -> Path:
    steam_root = tmp_path / "debian-installation"
    client = steam_root / "ubuntu12_32" / "steam"
    client.parent.mkdir(parents=True)
    client.write_text("")
    return steam_root


def test_finds_the_client_by_its_process_name(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    add_process(proc, 4242, "steam")

    assert is_steam_running(make_steam_root(tmp_path), proc)


def test_finds_the_client_by_its_binary_through_a_symlinked_steam_root(tmp_path: Path) -> None:
    steam_root = make_steam_root(tmp_path)
    linked_root = tmp_path / "steam-link"
    linked_root.symlink_to(steam_root)
    proc = tmp_path / "proc"
    add_process(proc, 4242, "renamed", steam_root / "ubuntu12_32" / "steam")

    assert is_steam_running(linked_root, proc)


def test_ignores_processes_that_only_mention_steam(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    add_process(proc, 100, "python3", Path("/usr/bin/python3"))
    add_process(proc, 101, "steamshare")
    add_process(proc, 102, "steamwebhelper")
    (proc / "self").mkdir()  # not a process folder

    assert not is_steam_running(make_steam_root(tmp_path), proc)


def test_skips_processes_that_end_or_hide_their_details(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "300").mkdir(parents=True)  # exited between listing and reading

    assert not is_steam_running(make_steam_root(tmp_path), proc)
