"""Tests for how the `doctor` command reports results and picks its exit code."""

from steamshare.cli.doctor import exit_code_for, format_result
from steamshare.core.doctor import CheckResult, Status


def make_result(status: Status) -> CheckResult:
    return CheckResult(title="A check", status=status)


def test_exit_code_is_zero_when_nothing_failed() -> None:
    results = [make_result(Status.OK), make_result(Status.WARNING)]

    assert exit_code_for(results) == 0


def test_exit_code_is_one_when_any_check_failed() -> None:
    results = [make_result(Status.OK), make_result(Status.FAILURE)]

    assert exit_code_for(results) == 1


def test_exit_code_is_zero_with_no_results() -> None:
    assert exit_code_for([]) == 0


def test_format_shows_marker_title_indented_details_and_hint() -> None:
    result = CheckResult(
        title="Library partition",
        status=Status.FAILURE,
        details=("first", "second"),
        hint="Run this\nthen that",
    )

    assert format_result(result) == (
        "[FAIL] Library partition\n"
        "       first\n"
        "       second\n"
        "       Next step: Run this\n"
        "                  then that"
    )


def test_format_of_a_passing_check_is_one_line() -> None:
    assert format_result(make_result(Status.OK)) == "[ OK ] A check"
