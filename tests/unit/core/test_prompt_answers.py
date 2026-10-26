"""Tests for reading the answers a user types at install's prompts."""

import pytest

from steamshare.core.prompt_answers import parse_numbered_choice, parse_yes_no


@pytest.mark.parametrize(("answer", "expected"), [("1", 0), ("3", 2), (" 2 ", 1)])
def test_a_number_in_range_picks_that_option(answer: str, expected: int) -> None:
    assert parse_numbered_choice(answer, option_count=3, default=None) == expected


@pytest.mark.parametrize("answer", ["0", "4", "-1", "two", "1.5"])
def test_anything_else_is_invalid(answer: str) -> None:
    assert parse_numbered_choice(answer, option_count=3, default=None) is None


def test_an_empty_answer_takes_the_default() -> None:
    assert parse_numbered_choice("", option_count=3, default=1) == 1


def test_an_empty_answer_is_invalid_without_a_default() -> None:
    assert parse_numbered_choice("  ", option_count=3, default=None) is None


@pytest.mark.parametrize(("answer", "expected"), [("y", True), ("YES", True), ("n", False)])
def test_yes_and_no_in_any_case(answer: str, expected: bool) -> None:
    assert parse_yes_no(answer, default=False) is expected


@pytest.mark.parametrize("default", [True, False])
def test_an_empty_yes_no_answer_takes_the_default(default: bool) -> None:
    assert parse_yes_no("", default=default) is default


def test_another_word_is_not_a_yes_no_answer() -> None:
    assert parse_yes_no("maybe", default=True) is None
