"""Tests for reading the app manifests of a library folder."""

from pathlib import Path

import pytest

from steamshare.adapters.app_manifests import ManifestError, read_library_apps
from steamshare.core.proton_mapping import LibraryApp


def write_manifest(library: Path, appid: str, name: str, installdir: str) -> None:
    steamapps = library / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        '"AppState"\n'
        "{\n"
        f'\t"appid"\t\t"{appid}"\n'
        f'\t"name"\t\t"{name}"\n'
        '\t"StateFlags"\t\t"4"\n'
        f'\t"installdir"\t\t"{installdir}"\n'
        "}\n",
        encoding="utf-8",
    )


def test_reads_every_manifest_sorted_by_file_name(tmp_path: Path) -> None:
    write_manifest(tmp_path, "1493710", "Proton Experimental", "Proton - Experimental")
    write_manifest(tmp_path, "1005870", "HYPERFIGHT", "HYPERFIGHT")
    (tmp_path / "steamapps" / "libraryfolder.vdf").write_text('"libraryfolder"\n{\n}\n')

    assert read_library_apps(tmp_path) == [
        LibraryApp("1005870", "HYPERFIGHT", "HYPERFIGHT"),
        LibraryApp("1493710", "Proton Experimental", "Proton - Experimental"),
    ]


def test_an_empty_library_has_no_apps(tmp_path: Path) -> None:
    (tmp_path / "steamapps").mkdir()

    assert read_library_apps(tmp_path) == []


def test_a_broken_manifest_is_reported_with_its_path(tmp_path: Path) -> None:
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    broken = steamapps / "appmanifest_400.acf"
    broken.write_text('"AppState"\n{\n\t"appid"\t\t"400"\n')

    with pytest.raises(ManifestError, match=str(broken)):
        read_library_apps(tmp_path)
