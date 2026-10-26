"""Tests for reading settings back from unit files steamshare generated."""

from pathlib import PurePosixPath

from steamshare.core.systemd_units import (
    GENERATED_HEADER,
    InstallConfig,
    render_bind_mount,
    render_ntfs3_loader_service,
    render_ntfs_mount,
)
from steamshare.core.unit_file_settings import has_section, read_path_setting, read_setting

CONFIG = InstallConfig(
    partition_uuid="1111AAAA2222BBBB",
    mount_point=PurePosixPath("/mnt/steamlib"),
    library_folder="SteamLibrary",
    user_id=1000,
    group_id=1000,
    home=PurePosixPath("/home/player"),
)


def test_reads_where_and_what_from_a_generated_bind_mount() -> None:
    content = render_bind_mount(CONFIG, "compatdata").content

    assert read_path_setting(content, "Mount", "Where") == PurePosixPath(
        "/mnt/steamlib/SteamLibrary/steamapps/compatdata"
    )
    assert read_path_setting(content, "Mount", "What") == PurePosixPath(
        "/home/player/.local/share/steam-shared/compatdata"
    )


def test_reads_a_setting_only_from_its_own_section() -> None:
    mount = render_ntfs_mount(CONFIG).content
    service = render_ntfs3_loader_service(CONFIG).content

    assert read_setting(mount, "Mount", "Type") == "ntfs3"
    # The loader's Type= is in [Service], not [Mount].
    assert read_setting(service, "Mount", "Type") is None
    assert read_setting(service, "Service", "Type") == "oneshot"


def test_a_missing_setting_reads_as_none() -> None:
    assert read_setting(render_ntfs_mount(CONFIG).content, "Mount", "Nothing") is None
    assert read_path_setting(render_ntfs_mount(CONFIG).content, "Mount", "Nothing") is None


def test_the_last_assignment_wins_as_in_systemd() -> None:
    content = "[Mount]\nWhere=/first\nWhere=/second\n"

    assert read_setting(content, "Mount", "Where") == "/second"


def test_comments_and_blank_lines_are_skipped() -> None:
    content = f"{GENERATED_HEADER}\n[Mount]\n# Where=/commented\n; Where=/also\n\nWhere=/real\n"

    assert read_setting(content, "Mount", "Where") == "/real"


def test_a_percent_in_a_path_reads_back_as_written_before_escaping() -> None:
    config = InstallConfig(
        partition_uuid="1111AAAA2222BBBB",
        mount_point=PurePosixPath("/mnt/100%games"),
        library_folder="SteamLibrary",
        user_id=1000,
        group_id=1000,
        home=PurePosixPath("/home/player"),
    )

    content = render_ntfs_mount(config).content

    assert read_path_setting(content, "Mount", "Where") == PurePosixPath("/mnt/100%games")


def test_tells_whether_a_unit_has_a_section() -> None:
    assert has_section(render_ntfs_mount(CONFIG).content, "Install")
    # The ntfs3 loader is started by the mount that requires it, never enabled.
    assert not has_section(render_ntfs3_loader_service(CONFIG).content, "Install")
