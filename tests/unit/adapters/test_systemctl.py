"""Tests for the commands sent to systemctl, without running systemctl."""

from collections.abc import Sequence

from steamshare.adapters.systemctl import (
    daemon_reload_command,
    disable_now_command,
    enable_units_now,
    plan_enable_commands,
    plan_restart_commands,
    run_commands,
    stop_command,
    user_systemctl,
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


def test_a_users_units_go_through_that_users_own_manager_even_from_root() -> None:
    assert plan_enable_commands(["w.service"], systemctl=user_systemctl("player")) == [
        ["systemctl", "--user", "--machine=player@", "daemon-reload"],
        ["systemctl", "--user", "--machine=player@", "enable", "--now", "w.service"],
    ]


def test_changed_units_are_restarted_so_they_run_their_new_command() -> None:
    assert plan_restart_commands(["w.service"], systemctl=user_systemctl("player")) == [
        ["systemctl", "--user", "--machine=player@", "restart", "w.service"],
    ]


def test_nothing_is_restarted_when_nothing_changed() -> None:
    assert plan_restart_commands([], systemctl=user_systemctl("player")) == []


def test_planned_commands_run_in_order() -> None:
    commands: list[list[str]] = []

    run_commands([["a"], ["b", "c"]], run=commands.append)

    assert commands == [["a"], ["b", "c"]]


def test_uninstall_commands_disable_stop_and_reload_one_unit_at_a_time() -> None:
    assert disable_now_command("a.mount") == ["systemctl", "disable", "--now", "a.mount"]
    assert stop_command("s.service") == ["systemctl", "stop", "s.service"]
    assert daemon_reload_command(systemctl=user_systemctl("player")) == [
        "systemctl",
        "--user",
        "--machine=player@",
        "daemon-reload",
    ]
