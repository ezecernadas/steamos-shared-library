"""Tests for the minimum Python version and the check the launcher runs with it."""

import sys
import tomllib
from pathlib import Path

from steamshare.python_requirement import MINIMUM_PYTHON_VERSION, unsupported_python_message

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_a_new_enough_python_passes() -> None:
    assert unsupported_python_message((3, 11, 0, "final", 0)) is None
    assert unsupported_python_message((3, 13, 2, "final", 0)) is None


def test_a_too_old_python_is_named_with_the_version_needed_and_the_next_step() -> None:
    message = unsupported_python_message((3, 9, 7, "final", 0))

    assert message is not None
    assert "needs Python 3.11 or newer" in message
    assert "3.9.7" in message
    assert "PATH" in message


def test_this_python_is_new_enough() -> None:
    # The tests themselves run on a supported Python (CI's matrix starts at the minimum).
    assert unsupported_python_message(sys.version_info) is None


def test_pyproject_requires_the_same_minimum() -> None:
    # pyproject.toml repeats the minimum for packaging tools; this keeps the two equal.
    requires_python = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]

    major, minor = MINIMUM_PYTHON_VERSION
    assert requires_python == f">={major}.{minor}"
