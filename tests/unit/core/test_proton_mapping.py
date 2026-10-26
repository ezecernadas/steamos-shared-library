"""Tests for deciding which library apps get a forced Proton mapping.

The policy: every game in the shared library is mapped to Proton, so Linux
Steam keeps the Windows build there. Tools, the Steamworks redistributables,
apps the user keeps native, and mappings the user made are left alone, and
steamshare only ever removes the mappings it created itself.
"""

import pytest

from steamshare.core.proton_mapping import (
    LibraryApp,
    MappingAction,
    SteamshareState,
    allow_proton,
    mark_keep_native,
    plan_proton_mappings,
    read_compat_tool_mappings,
    read_library_app,
    with_compat_tool_mappings,
)
from steamshare.core.vdf import KeyValues

TOOL = "proton_experimental"

HYPERFIGHT = LibraryApp(appid="1005870", name="HYPERFIGHT", installdir="HYPERFIGHT")
PROTON = LibraryApp(appid="1493710", name="Proton Experimental", installdir="Proton - Experimental")
RUNTIME = LibraryApp(
    appid="4183110", name="Steam Linux Runtime 4.0", installdir="SteamLinuxRuntime_4"
)
REDISTRIBUTABLES = LibraryApp(
    appid="228980", name="Steamworks Common Redistributables", installdir="Steamworks Shared"
)
PORTAL = LibraryApp(appid="400", name="Portal", installdir="Portal")


def steamshare_entry(tool: str = TOOL) -> KeyValues:
    return {"name": tool, "config": "", "priority": "250"}


def actions_by_appid(plan_decisions) -> dict[str, MappingAction]:
    return {decision.app.appid: decision.action for decision in plan_decisions}


# --- Manifests -----------------------------------------------------------------


def test_reads_the_app_record_from_a_parsed_manifest() -> None:
    manifest = {"AppState": {"appid": "400", "name": "Portal", "installdir": "Portal"}}

    assert read_library_app(manifest) == PORTAL


def test_a_manifest_without_the_needed_keys_is_rejected_with_the_missing_key() -> None:
    with pytest.raises(ValueError, match="installdir"):
        read_library_app({"AppState": {"appid": "400", "name": "Portal"}})


# --- Planning ------------------------------------------------------------------


def test_maps_a_game_that_has_no_mapping() -> None:
    plan = plan_proton_mappings([PORTAL], {}, SteamshareState(), TOOL)

    assert actions_by_appid(plan.decisions) == {"400": MappingAction.MAP}
    assert plan.mappings == {"400": steamshare_entry()}
    assert plan.state.managed_mappings == frozenset({"400"})


def test_uses_the_requested_tool() -> None:
    plan = plan_proton_mappings([PORTAL], {}, SteamshareState(), "proton_9")

    assert plan.mappings == {"400": steamshare_entry("proton_9")}


@pytest.mark.parametrize("tool_app", [PROTON, RUNTIME])
def test_skips_the_linux_tools_steam_installs_into_the_library(tool_app: LibraryApp) -> None:
    plan = plan_proton_mappings([tool_app], {}, SteamshareState(), TOOL)

    assert actions_by_appid(plan.decisions) == {tool_app.appid: MappingAction.SKIPPED_TOOL}
    assert plan.mappings == {}


def test_skips_the_steamworks_redistributables() -> None:
    plan = plan_proton_mappings([REDISTRIBUTABLES], {}, SteamshareState(), TOOL)

    assert actions_by_appid(plan.decisions) == {"228980": MappingAction.SKIPPED_TOOL}
    assert plan.mappings == {}


def test_skips_an_app_kept_native() -> None:
    state = SteamshareState(keep_native=frozenset({"400"}))

    plan = plan_proton_mappings([PORTAL], {}, state, TOOL)

    assert actions_by_appid(plan.decisions) == {"400": MappingAction.SKIPPED_KEEP_NATIVE}
    assert plan.mappings == {}


def test_never_touches_a_mapping_the_user_made() -> None:
    user_mapping = {"1005870": steamshare_entry("proton_8")}

    plan = plan_proton_mappings([HYPERFIGHT], user_mapping, SteamshareState(), TOOL)

    assert actions_by_appid(plan.decisions) == {"1005870": MappingAction.SKIPPED_USER_MAPPING}
    assert plan.mappings == user_mapping
    assert plan.state.managed_mappings == frozenset()


def test_reports_a_mapping_steamshare_already_made() -> None:
    state = SteamshareState(managed_mappings=frozenset({"400"}))
    mappings = {"400": steamshare_entry()}

    plan = plan_proton_mappings([PORTAL], mappings, state, TOOL)

    assert actions_by_appid(plan.decisions) == {"400": MappingAction.ALREADY_MAPPED}
    assert plan.mappings == mappings
    assert plan.state == state


def test_removes_a_managed_mapping_when_the_app_is_kept_native() -> None:
    state = SteamshareState(keep_native=frozenset({"400"}), managed_mappings=frozenset({"400"}))

    plan = plan_proton_mappings([PORTAL], {"400": steamshare_entry()}, state, TOOL)

    assert actions_by_appid(plan.decisions) == {"400": MappingAction.UNMAP}
    assert plan.mappings == {}
    assert plan.state.managed_mappings == frozenset()


def test_keeps_a_user_mapping_even_when_the_app_is_kept_native() -> None:
    state = SteamshareState(keep_native=frozenset({"400"}))
    user_mapping = {"400": steamshare_entry("proton_8")}

    plan = plan_proton_mappings([PORTAL], user_mapping, state, TOOL)

    assert actions_by_appid(plan.decisions) == {"400": MappingAction.SKIPPED_KEEP_NATIVE}
    assert plan.mappings == user_mapping


def test_keeps_mappings_of_apps_outside_the_library() -> None:
    other_library = {"570": steamshare_entry("proton_8")}
    state = SteamshareState(managed_mappings=frozenset({"999"}))

    plan = plan_proton_mappings([PORTAL], other_library, state, TOOL)

    assert plan.mappings == {**other_library, "400": steamshare_entry()}
    assert plan.state.managed_mappings == frozenset({"999", "400"})


def test_planning_twice_changes_nothing_the_second_time() -> None:
    apps = [HYPERFIGHT, PROTON, RUNTIME, REDISTRIBUTABLES, PORTAL]
    first = plan_proton_mappings(apps, {}, SteamshareState(), TOOL)

    second = plan_proton_mappings(apps, first.mappings, first.state, TOOL)

    assert second.mappings == first.mappings
    assert second.state == first.state
    assert MappingAction.MAP not in actions_by_appid(second.decisions).values()


def test_does_not_modify_its_inputs() -> None:
    mappings: KeyValues = {}

    plan_proton_mappings([PORTAL], mappings, SteamshareState(), TOOL)

    assert mappings == {}


# --- State changes requested on the command line -------------------------------


def test_marking_keep_native_adds_the_apps() -> None:
    state = mark_keep_native(SteamshareState(keep_native=frozenset({"1"})), ["400"])

    assert state.keep_native == frozenset({"1", "400"})


def test_allowing_proton_removes_the_apps_from_keep_native() -> None:
    state = allow_proton(SteamshareState(keep_native=frozenset({"1", "400"})), ["400"])

    assert state.keep_native == frozenset({"1"})


# --- The CompatToolMapping node in config.vdf ----------------------------------


def make_config(steam_section: KeyValues) -> KeyValues:
    return {"InstallConfigStore": {"Software": {"Valve": {"Steam": steam_section}}}}


def test_reads_the_existing_mappings() -> None:
    config = make_config({"CompatToolMapping": {"400": steamshare_entry()}})

    assert read_compat_tool_mappings(config) == {"400": steamshare_entry()}


def test_reads_no_mappings_when_the_node_is_missing() -> None:
    assert read_compat_tool_mappings({"InstallConfigStore": {}}) == {}


def test_finds_the_node_whatever_the_case_of_the_keys() -> None:
    # Steam matches keys case-insensitively, and some installs write "valve".
    config = {"InstallConfigStore": {"Software": {"valve": {"Steam": {"CompatToolMapping": {}}}}}}

    assert with_compat_tool_mappings(config, {"400": steamshare_entry()}) == {
        "InstallConfigStore": {
            "Software": {"valve": {"Steam": {"CompatToolMapping": {"400": steamshare_entry()}}}}
        }
    }


def test_replaces_the_mappings_in_place_keeping_the_other_keys_and_their_order() -> None:
    config = make_config({"CompatToolMapping": {}, "AutoUpdateWindowEnabled": "0"})

    updated = with_compat_tool_mappings(config, {"400": steamshare_entry()})

    steam = updated["InstallConfigStore"]["Software"]["Valve"]["Steam"]
    assert list(steam) == ["CompatToolMapping", "AutoUpdateWindowEnabled"]
    assert steam["CompatToolMapping"] == {"400": steamshare_entry()}
    assert config == make_config({"CompatToolMapping": {}, "AutoUpdateWindowEnabled": "0"})


def test_creates_the_path_to_the_node_when_it_is_missing() -> None:
    updated = with_compat_tool_mappings({"InstallConfigStore": {}}, {"400": steamshare_entry()})

    assert updated == make_config({"CompatToolMapping": {"400": steamshare_entry()}})


def test_rejects_a_config_where_a_section_is_a_plain_value() -> None:
    with pytest.raises(ValueError, match="Software"):
        with_compat_tool_mappings({"InstallConfigStore": {"Software": "oops"}}, {})
