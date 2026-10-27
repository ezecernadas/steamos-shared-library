"""Tests for release/install.sh, run as bash runs it, up to where it would need root.

Past the root check it downloads and installs, which a unit test must not
do; `steamshare.release_setup`, which holds its decisions, is tested on its
own (tests/unit/test_release_setup.py).
"""

import os
import subprocess
from pathlib import Path

import pytest
from build_assets import INSTALLER_ASSET, build_release_assets

REPOSITORY = Path(__file__).resolve().parents[3]
TEMPLATE = REPOSITORY / "release" / "install.sh"


def run_bash(script: Path) -> subprocess.CompletedProcess[str]:
    # Through bash's standard input, as `curl ... | bash` runs it.
    return subprocess.run(
        ["bash"],
        input=script.read_text(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_the_template_refuses_to_run() -> None:
    result = run_bash(TEMPLATE)

    assert result.returncode == 1
    assert "unbuilt template" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the message shown without root")
def test_the_built_installer_says_to_use_sudo(tmp_path: Path) -> None:
    build_release_assets(REPOSITORY, tmp_path, tag=None)

    result = run_bash(tmp_path / INSTALLER_ASSET)

    assert result.returncode == 1
    assert "needs root" in result.stderr
    assert "| sudo bash" in result.stderr
    assert "Downloading" not in result.stdout
