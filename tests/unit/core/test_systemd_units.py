"""Tests for the systemd units `install` generates.

The units are plain text, so the tests check unit names and the exact key
lines that make the setup work: the driver and options of the NTFS mount, the
explicit `modprobe` that bypasses the distro blacklist, and the bind mounts
that follow the NTFS mount up and down.
"""

from pathlib import PurePosixPath

import pytest

from steamshare.core.systemd_units import (
    GENERATED_HEADER,
    NTFS3_LOADER_SERVICE,
    WATCH_SERVICE,
    InstallConfig,
    UnitFile,
    WatchServiceConfig,
    build_units,
    escape_unit_path,
    generated_header,
    linux_only_data_paths,
    mount_unit_name,
    quote_exec_argument,
    render_bind_mount,
    render_ntfs3_loader_service,
    render_ntfs_mount,
    render_watch_service,
)

CONFIG = InstallConfig(
    partition_uuid="D8E665D5E665B502",
    mount_point=PurePosixPath("/mnt/steamlib"),
    library_folder="SteamLibrary",
    user_id=1000,
    group_id=1000,
    home=PurePosixPath("/home/ezecer"),
)

NTFS_MOUNT_UNIT = "mnt-steamlib.mount"


def lines_of(unit: UnitFile) -> list[str]:
    return unit.content.splitlines()


# --- Unit name escaping ----------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # Both examples come from `systemd-escape --path` (systemd 255).
        (
            "/mnt/steamlib/SteamLibrary/steamapps/compatdata",
            "mnt-steamlib-SteamLibrary-steamapps-compatdata",
        ),
        ("/mnt/steam lib/Steam-Library", "mnt-steam\\x20lib-Steam\\x2dLibrary"),
        ("/.hidden/dir", "\\x2ehidden-dir"),
        ("/mnt/café", "mnt-caf\\xc3\\xa9"),
        ("/mnt/a:b_c.d", "mnt-a:b_c.d"),
        ("/", "-"),
    ],
)
def test_escape_unit_path_matches_systemd_escape(path: str, expected: str) -> None:
    assert escape_unit_path(PurePosixPath(path)) == expected


def test_mount_unit_name_is_the_escaped_path_with_the_mount_suffix() -> None:
    assert mount_unit_name(PurePosixPath("/mnt/steamlib")) == NTFS_MOUNT_UNIT


# --- Every unit --------------------------------------------------------------


def test_build_units_returns_loader_ntfs_mount_and_one_bind_mount_per_linux_only_folder() -> None:
    names = [unit.name for unit in build_units(CONFIG)]

    assert names == [
        NTFS3_LOADER_SERVICE,
        NTFS_MOUNT_UNIT,
        "mnt-steamlib-SteamLibrary-steamapps-compatdata.mount",
        "mnt-steamlib-SteamLibrary-steamapps-shadercache.mount",
    ]


def test_every_unit_starts_with_the_generated_header() -> None:
    for unit in build_units(CONFIG):
        assert unit.content.startswith(GENERATED_HEADER)


def test_only_the_mounts_are_enabled_because_they_pull_in_the_loader() -> None:
    enabled = [unit.name for unit in build_units(CONFIG) if unit.enable_at_boot]

    assert NTFS3_LOADER_SERVICE not in enabled
    assert NTFS_MOUNT_UNIT in enabled
    assert len(enabled) == 3


# --- ntfs3 loader ------------------------------------------------------------


def test_loader_runs_modprobe_once_and_stays_active() -> None:
    unit = render_ntfs3_loader_service(CONFIG)
    lines = lines_of(unit)

    assert unit.name == "steamshare-ntfs3.service"
    assert "Type=oneshot" in lines
    assert "RemainAfterExit=yes" in lines
    assert "ExecStart=/usr/sbin/modprobe ntfs3" in lines


def test_loader_runs_early_and_before_the_ntfs_mount() -> None:
    lines = lines_of(render_ntfs3_loader_service(CONFIG))

    assert "DefaultDependencies=no" in lines
    assert f"Before={NTFS_MOUNT_UNIT}" in lines


def test_loader_has_no_install_section_because_the_mount_requires_it() -> None:
    assert "[Install]" not in lines_of(render_ntfs3_loader_service(CONFIG))


# --- NTFS mount --------------------------------------------------------------


def test_ntfs_mount_uses_ntfs3_by_uuid_with_windows_names_and_nofail() -> None:
    unit = render_ntfs_mount(CONFIG)
    lines = lines_of(unit)

    assert unit.name == NTFS_MOUNT_UNIT
    assert "What=/dev/disk/by-uuid/D8E665D5E665B502" in lines
    assert "Where=/mnt/steamlib" in lines
    assert "Type=ntfs3" in lines
    assert "Options=uid=1000,gid=1000,umask=022,windows_names,nofail" in lines


def test_ntfs_mount_requires_the_loader_and_starts_at_boot() -> None:
    lines = lines_of(render_ntfs_mount(CONFIG))

    assert f"Requires={NTFS3_LOADER_SERVICE}" in lines
    assert f"After={NTFS3_LOADER_SERVICE}" in lines
    assert "WantedBy=local-fs.target" in lines


def test_ntfs_mount_owner_comes_from_the_config() -> None:
    config = InstallConfig(
        partition_uuid="ABCD",
        mount_point=PurePosixPath("/mnt/games"),
        library_folder="SteamLibrary",
        user_id=1001,
        group_id=1002,
        home=PurePosixPath("/home/deck"),
    )

    assert "Options=uid=1001,gid=1002,umask=022,windows_names,nofail" in lines_of(
        render_ntfs_mount(config)
    )


# --- Bind mounts -------------------------------------------------------------


def test_bind_mount_maps_the_ext4_folder_over_the_library_folder() -> None:
    unit = render_bind_mount(CONFIG, "compatdata")
    lines = lines_of(unit)

    assert unit.name == "mnt-steamlib-SteamLibrary-steamapps-compatdata.mount"
    assert "What=/home/ezecer/.local/share/steam-shared/compatdata" in lines
    assert "Where=/mnt/steamlib/SteamLibrary/steamapps/compatdata" in lines
    assert "Type=none" in lines
    assert "Options=bind,nofail" in lines


def test_bind_mount_lives_and_dies_with_the_ntfs_mount() -> None:
    lines = lines_of(render_bind_mount(CONFIG, "shadercache"))

    assert f"BindsTo={NTFS_MOUNT_UNIT}" in lines
    assert f"After={NTFS_MOUNT_UNIT}" in lines
    assert "WantedBy=local-fs.target" in lines


def test_linux_only_data_paths_are_absolute_under_home() -> None:
    assert linux_only_data_paths(CONFIG) == (
        PurePosixPath("/home/ezecer/.local/share/steam-shared/compatdata"),
        PurePosixPath("/home/ezecer/.local/share/steam-shared/shadercache"),
    )


# --- Specifiers --------------------------------------------------------------


def test_percent_signs_in_paths_are_escaped_so_systemd_does_not_expand_them() -> None:
    config = InstallConfig(
        partition_uuid="ABCD",
        mount_point=PurePosixPath("/mnt/100%"),
        library_folder="SteamLibrary",
        user_id=1000,
        group_id=1000,
        home=PurePosixPath("/home/ezecer"),
    )

    assert "Where=/mnt/100%%" in lines_of(render_ntfs_mount(config))


# --- The watch service ---------------------------------------------------------

WATCH_CONFIG = WatchServiceConfig(
    python=PurePosixPath("/home/ezecer/steamshare/.venv/bin/python"),
    library=PurePosixPath("/mnt/steamlib/SteamLibrary"),
    tool="proton_experimental",
    steam_root=PurePosixPath("/home/ezecer/.steam/steam"),
)


def test_the_watch_service_runs_watch_with_this_python_for_the_library() -> None:
    unit = render_watch_service(WATCH_CONFIG)

    assert unit.name == WATCH_SERVICE
    assert unit.enable_at_boot
    assert (
        "ExecStart=/home/ezecer/steamshare/.venv/bin/python -m steamshare watch "
        "--library /mnt/steamlib/SteamLibrary --tool proton_experimental "
        "--steam-root /home/ezecer/.steam/steam"
    ) in lines_of(unit)


def test_the_watch_service_restarts_on_failure_and_starts_with_the_session() -> None:
    lines = lines_of(render_watch_service(WATCH_CONFIG))

    assert "Restart=on-failure" in lines
    assert "WantedBy=default.target" in lines


def test_the_watch_service_says_which_command_generated_it() -> None:
    unit = render_watch_service(WATCH_CONFIG)

    assert unit.content.startswith(generated_header("install-watch"))
    assert "`python -m steamshare install-watch`" in unit.content


def test_install_units_keep_their_header() -> None:
    assert generated_header("install") == GENERATED_HEADER


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("/mnt/steam lib", '"/mnt/steam lib"'),
        ("/mnt/100%", "/mnt/100%%"),
        ("/mnt/$HOME", "/mnt/$$HOME"),
        ('/mnt/a"b', '"/mnt/a\\"b"'),
        ("/mnt/a\\b", '"/mnt/a\\\\b"'),
    ],
)
def test_exec_arguments_are_quoted_so_systemd_reads_them_literally(
    argument: str, expected: str
) -> None:
    assert quote_exec_argument(argument) == expected
