"""Tests for the commands sent to systemctl, without running systemctl."""

from collections.abc import Sequence

from steamshare.adapters.systemctl import enable_units_now, plan_enable_commands


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
