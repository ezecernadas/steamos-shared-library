"""Tests for the `steamshare` launcher at the repository root, run as users run it.

They start the real bash script in a subprocess. That is I/O, but it is fast,
needs no root and touches nothing outside a temporary folder, so it belongs
with the unit tests and runs on every `pytest`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
LAUNCHER = REPOSITORY / "steamshare"

# Stands in for an old python3: the real interpreter, reporting 3.9 to the
# version check it runs.
FAKE_OLD_PYTHON = f"""#!/bin/sh
exec {sys.executable} -c '
import runpy, sys
sys.version_info = (3, 9, 0, "final", 0)
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
' "$@"
"""


def run_launcher(
    *arguments: str, path: str, launcher: Path = LAUNCHER
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PATH": path}
    return subprocess.run(
        [str(launcher), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def path_with_this_python_first(tmp_path: Path) -> str:
    # The python3 that runs the tests is a supported one; make it the one found.
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    (bin_directory / "python3").symlink_to(sys.executable)
    return f"{bin_directory}{os.pathsep}{os.environ['PATH']}"


def test_help_lists_the_commands(tmp_path: Path) -> None:
    result = run_launcher("--help", path=path_with_this_python_first(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "install" in result.stdout
    assert "doctor" in result.stdout
    assert "install-watch" not in result.stdout


def test_a_symlink_to_the_launcher_still_finds_the_repository(tmp_path: Path) -> None:
    link = tmp_path / "elsewhere" / "steamshare"
    link.parent.mkdir()
    link.symlink_to(LAUNCHER)

    result = run_launcher("--help", path=path_with_this_python_first(tmp_path), launcher=link)

    assert result.returncode == 0, result.stderr


def test_a_too_old_python_gets_a_clear_message(tmp_path: Path) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    fake_python = bin_directory / "python3"
    fake_python.write_text(FAKE_OLD_PYTHON)
    fake_python.chmod(0o755)

    result = run_launcher("--help", path=f"{bin_directory}{os.pathsep}{os.environ['PATH']}")

    assert result.returncode != 0
    assert "needs Python 3.11 or newer" in result.stderr
    assert "3.9.0" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_missing_python_gets_a_clear_message(tmp_path: Path) -> None:
    # Only what the launcher needs besides python3, so python3 is not found.
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for tool in ("bash", "readlink", "dirname"):
        found = shutil.which(tool)
        assert found is not None
        (bin_directory / tool).symlink_to(found)

    result = run_launcher("--help", path=str(bin_directory))

    assert result.returncode != 0
    assert "python3" in result.stderr
    assert "PATH" in result.stderr
