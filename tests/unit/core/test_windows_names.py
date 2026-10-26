"""Tests for the rules that decide whether a file name is valid on Windows.

These names matter because the Linux `ntfs3` driver can write names that
Windows itself refuses. Windows then reports those files as corruption.
"""

import pytest

from steamshare.core.windows_names import find_name_violation


@pytest.mark.parametrize(
    "name",
    [
        "game.exe",
        "Proton - Experimental",
        "appmanifest_1005870.acf",
        "c",
        "console.log",  # starts with a reserved name but is not one
        "auxiliary",
        ".hidden",  # a leading dot is fine; only a trailing one is not
        "ñandú.txt",
    ],
)
def test_accepts_names_that_windows_allows(name: str) -> None:
    assert find_name_violation(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "c:",  # what Proton creates in dosdevices/
        "what?",
        "a*b",
        'say"hi"',
        "a<b>c",
        "pipe|name",
        "back\\slash",
    ],
)
def test_rejects_reserved_characters(name: str) -> None:
    violation = find_name_violation(name)

    assert violation is not None
    assert "reserved character" in violation.reason


def test_rejects_control_characters() -> None:
    violation = find_name_violation("tab\tname")

    assert violation is not None
    assert "control character" in violation.reason


@pytest.mark.parametrize(
    "name",
    ["CON", "aux", "Nul", "PRN", "COM1", "lpt9", "COM¹", "AUX.txt", "nul.tar.gz"],
)
def test_rejects_reserved_device_names(name: str) -> None:
    violation = find_name_violation(name)

    assert violation is not None
    assert "reserved device name" in violation.reason


@pytest.mark.parametrize("name", ["trailing.", "trailing "])
def test_rejects_trailing_dot_or_space(name: str) -> None:
    violation = find_name_violation(name)

    assert violation is not None
    assert "ends with" in violation.reason


def test_violation_keeps_the_offending_name() -> None:
    violation = find_name_violation("c:")

    assert violation is not None
    assert violation.name == "c:"
