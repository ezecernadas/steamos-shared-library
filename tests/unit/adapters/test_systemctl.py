"""Tests for the commands sent to systemctl, without running systemctl."""

from collections.abc import Sequence

from steamshare.adapters.systemctl import (
    USER_SYSTEMCTL,
    enable_units_now,
    plan_enable_commands,
)


def test_reloads_systemd_then_enables_and_starts_the_units() -> None:
    assert plan_enable_commands(["a.mount", "b.mount"]) == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "a.mount", "b.mount"],
    ]


def test_enabling_runs_the_planned_commands_in_order() -> None:
    commands: list[list[str]] = []

    def record(command: Sequence[str]) -> None:
        commands.append(list(command))

    enable_units_now(["a.mount"], run=record)

    assert commands == plan_enable_commands(["a.mount"])


def test_user_units_go_through_the_users_own_systemd() -> None:
    assert plan_enable_commands(["w.service"], systemctl=USER_SYSTEMCTL) == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "w.service"],
    ]


def test_enabling_user_units_runs_the_user_commands() -> None:
    commands: list[list[str]] = []

    enable_units_now(["w.service"], run=commands.append, systemctl=USER_SYSTEMCTL)

    assert commands == plan_enable_commands(["w.service"], systemctl=USER_SYSTEMCTL)
