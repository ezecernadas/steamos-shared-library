"""Find the unit files steamshare generated, with the backups install made of them.

`uninstall` must remove exactly what install wrote, and nothing the user
wrote. So a file counts only when it is a unit whose content starts with
steamshare's header (`systemd_units.is_generated_unit_file`); a name that
looks like ours is not enough.

The folder is a parameter: /etc/systemd/system in production, a temporary
folder in tests.
"""

from pathlib import Path

from steamshare.adapters.safe_writes import BACKUP_NAME_INFIX
from steamshare.core.systemd_units import is_generated_unit_file
from steamshare.core.uninstall_plan import InstalledUnit


def find_generated_units(directory: Path) -> list[InstalledUnit]:
    """Return every unit steamshare generated in the folder, sorted by name."""
    if not directory.is_dir():
        return []
    found = (find_generated_unit(path) for path in sorted(directory.iterdir()))
    return [unit for unit in found if unit is not None]


def find_generated_unit(path: Path) -> InstalledUnit | None:
    """Return the unit at `path` when steamshare generated it, else None."""
    # install writes its units as regular files. Links in a unit folder are
    # systemd's (enablement links, masks pointing at /dev/null) or the user's.
    if path.is_symlink() or not path.is_file():
        return None
    content = _read_text(path)
    if content is None or not is_generated_unit_file(path.name, content):
        return None
    return InstalledUnit(path=path, content=content, backups=_find_backups(path))


def _read_text(path: Path) -> str | None:
    # A file that cannot be read as text cannot be one steamshare wrote.
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _find_backups(unit_path: Path) -> tuple[Path, ...]:
    # Backups sit next to the unit as `<name>.bak-<timestamp>` (see `safe_writes`).
    prefix = unit_path.name + BACKUP_NAME_INFIX
    return tuple(
        sorted(
            path
            for path in unit_path.parent.iterdir()
            if path.name.startswith(prefix) and path.is_file() and not path.is_symlink()
        )
    )
