"""Find the user's config folder, as the XDG Base Directory spec defines it.

steamshare's state file lives there. `force-proton` and `watch` both use it,
and must agree on where "there" is, so the rule is written once. (`install`
does not use it for the user's unit folder: it runs as root, without the
user's environment; see `watch_service_setup`.)
"""

from collections.abc import Mapping
from pathlib import Path

# XDG Base Directory spec: user config goes to $XDG_CONFIG_HOME, or to
# ~/.config when it is unset or empty. systemd follows the same rule for
# user units.
XDG_CONFIG_HOME_VARIABLE = "XDG_CONFIG_HOME"
DEFAULT_CONFIG_HOME = Path(".config")


def user_config_home(environment: Mapping[str, str], home: Path) -> Path:
    config_home = environment.get(XDG_CONFIG_HOME_VARIABLE)
    return Path(config_home) if config_home else home / DEFAULT_CONFIG_HOME
