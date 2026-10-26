"""Read the app manifests of a library folder into `LibraryApp` records.

`force-proton` decides per app, and Steam's manifests are the list of what is
installed: one `steamapps/appmanifest_<appid>.acf` per app, whichever OS
installed it.
"""

from pathlib import Path

from steamshare.core.library_layout import APP_MANIFEST_PATTERN, STEAMAPPS_DIRECTORY
from steamshare.core.proton_mapping import LibraryApp, read_library_app
from steamshare.core.vdf import parse_vdf


class ManifestError(Exception):
    """A manifest could not be read; the message names the file."""


def read_library_apps(library: Path) -> list[LibraryApp]:
    """Return one record per manifest, sorted by file name so output is stable."""
    manifests = sorted((library / STEAMAPPS_DIRECTORY).glob(APP_MANIFEST_PATTERN))
    return [_read_manifest(path) for path in manifests]


def _read_manifest(path: Path) -> LibraryApp:
    try:
        return read_library_app(parse_vdf(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ManifestError(f"{path}: {error}") from error
